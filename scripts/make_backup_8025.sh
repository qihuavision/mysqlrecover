#!/bin/bash
# ============================================================
# 生成 MySQL 8.0.25 测试备份（Sprint 7：redo 分代验证用）
#
# 在恢复机上执行：启动临时 mysql:8.0.25 容器（datadir 挂载宿主机）
# → 建 pay 库插 150 条 transactions → xtrabackup 备份 → 清理容器
#
# 用途：8.0.25 属于 redo 第二代（8.0.20-29），与 8.0.3x 不能共用
# 恢复环境。用它造真实 8.0.25 备份验证分代隔离——批次 #11 实测
# pay.transactions=150 由 8.0.25 容器验出，与 8.0.3x 完全隔离。
#
# 模式同 make_test_backup_proven.sh（v6：挂载 datadir + 关 binlog）
# ============================================================
set -e

BACKUP_DIR="/data/drill/test-backups/test-instance-8025"
SOURCE_DATADIR="/data/drill/test-source-datadir-8025"
MYSQL_CONTAINER="test-source-mysql-8025"
MYSQL_ROOT_PWD="Test@123456"
SOURCE_PORT=3306

echo "===== 生成 MySQL 8.0.25 测试备份 ====="

# ---------- 1. 启动源 MySQL 容器（network host + datadir 挂载 + 关 binlog）----------
echo "1. 启动 mysql:8.0.25（datadir 挂载宿主机）..."
docker rm -f $MYSQL_CONTAINER 2>/dev/null || true
rm -rf "$SOURCE_DATADIR"
mkdir -p "$SOURCE_DATADIR"

docker run -d --network host --name $MYSQL_CONTAINER \
    -e MYSQL_ROOT_PASSWORD=$MYSQL_ROOT_PWD \
    -v "$SOURCE_DATADIR:/var/lib/mysql" \
    mysql:8.0.25 \
    --port=$SOURCE_PORT \
    --mysqlx=0 \
    --skip-log-bin \
    --default-authentication-plugin=mysql_native_password

echo "   等待初始化（40秒）..."
sleep 40

if docker exec $MYSQL_CONTAINER mysql -uroot -p"$MYSQL_ROOT_PWD" -h127.0.0.1 -P$SOURCE_PORT -e "SELECT VERSION()" 2>/dev/null; then
    echo "   ✅ MySQL 就绪"
else
    echo "   ❌ 未就绪"
    docker logs $MYSQL_CONTAINER 2>&1 | tail -15
    exit 1
fi

# ---------- 2. 建库建表插数据（pay.transactions 150 条）----------
echo "2. 创建测试数据..."
docker exec $MYSQL_CONTAINER mysql -uroot -p"$MYSQL_ROOT_PWD" -h127.0.0.1 -P$SOURCE_PORT -e "
CREATE DATABASE pay;
USE pay;
CREATE TABLE transactions (id BIGINT AUTO_INCREMENT PRIMARY KEY, tx_no VARCHAR(32), amount DECIMAL(10,2), status VARCHAR(16) DEFAULT 'OK', created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
INSERT INTO transactions (tx_no, amount) SELECT CONCAT('TX', LPAD(seq, 9, '0')), ROUND(RAND()*500, 2) FROM (SELECT a.N + b.N*10 + c.N*100 AS seq FROM (SELECT 0 N UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9) a, (SELECT 0 N UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9) b, (SELECT 0 N UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9) c) n LIMIT 150;
SELECT 'transactions' t, COUNT(*) cnt FROM transactions;
" 2>&1

# ---------- 3. xtrabackup 备份（容器化 MySQL 必须显式 --datadir 指宿主机挂载路径）----------
echo "3. xtrabackup 备份..."
mkdir -p "$BACKUP_DIR"
rm -rf "$BACKUP_DIR"/*

xtrabackup --backup \
    --datadir="$SOURCE_DATADIR" \
    --target-dir="$BACKUP_DIR" \
    --user=root \
    --password="$MYSQL_ROOT_PWD" \
    --host=127.0.0.1 \
    --port=$SOURCE_PORT 2>&1 | tail -5

# ---------- 4. 验证备份完整性 ----------
echo "4. 验证..."
if [ -f "$BACKUP_DIR/xtrabackup_checkpoints" ]; then
    echo "   ✅ 备份完整！"
    cat "$BACKUP_DIR/xtrabackup_checkpoints"
    echo "   大小: $(du -sh $BACKUP_DIR | awk '{print $1}')"
    echo "   文件数: $(find $BACKUP_DIR -type f | wc -l)"
else
    echo "   ❌ 不完整"
    exit 1
fi

# ---------- 5. 清理临时容器 ----------
echo "5. 清理容器..."
docker rm -f $MYSQL_CONTAINER
rm -rf "$SOURCE_DATADIR"

echo ""
echo "===== 完成: $BACKUP_DIR ====="
echo "备份 MySQL 版本: 8.0.25（redo 第二代 8.0.20-29）"
echo ""
echo "可将其加入 instances.yaml 用于分代隔离测试："
echo "  instances:"
echo "    - name: test-instance-8025"
echo "      host: 127.0.0.1"
echo "      port: $SOURCE_PORT"
echo "      mysql_version: \"8.0.25\""
echo "      backup_source_host: <本机IP>"
echo "      backup_source_path: $BACKUP_DIR"
echo ""
echo "验证要点：8.0.25 备份必须路由到 8.0.25 容器（独立端口），"
echo "与 8.0.3x（第三代）完全隔离；恢复后 pay.transactions COUNT=150。"
