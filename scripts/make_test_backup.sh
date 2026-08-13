#!/bin/bash
# ============================================================
# 生成测试备份（Sprint 1 联调用）
#
# 在恢复机上执行：启动一个临时 MySQL 容器 → 建库建表插数据
# → 用 xtrabackup 备份 → 得到一个可用于演练的小备份
#
# 用途：没有真实生产备份时，用它造一个测试备份跑通 drill run
# ============================================================
set -e

BACKUP_DIR="/data/drill/test-backups/test-instance-1"
MYSQL_CONTAINER="test-source-mysql"
MYSQL_ROOT_PWD="Test@123456"

echo "===== 生成测试备份 ====="

# ---------- 1. 启动源 MySQL 容器（造数据用）----------
echo "1. 启动临时 MySQL 容器..."
docker rm -f $MYSQL_CONTAINER 2>/dev/null || true
docker run -d --network host --name $MYSQL_CONTAINER \
    -e MYSQL_ROOT_PASSWORD=$MYSQL_ROOT_PWD \
    mysql:8.0.35 --port=13307 --mysqlx=0

echo "   等待 MySQL 启动..."
for i in $(seq 1 60); do
    if docker exec $MYSQL_CONTAINER mysqladmin ping -uroot -p"$MYSQL_ROOT_PWD" --silent 2>/dev/null; then
        echo "   ✅ MySQL 已就绪"
        break
    fi
    sleep 2
    if [ $i -eq 60 ]; then
        echo "   ❌ MySQL 启动超时"
        exit 1
    fi
done

# ---------- 2. 建库建表插数据 ----------
echo "2. 创建测试数据..."
docker exec $MYSQL_CONTAINER mysql -uroot -p"$MYSQL_ROOT_PWD" <<SQL
CREATE DATABASE IF NOT EXISTS orders;
USE orders;
CREATE TABLE IF NOT EXISTS order_info (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_no VARCHAR(32) NOT NULL,
    amount DECIMAL(10,2),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS customer (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(64),
    phone VARCHAR(20)
);
-- 插入 100 条订单数据
INSERT INTO order_info (order_no, amount)
    SELECT CONCAT('ORD', LPAD(seq, 8, '0')), ROUND(RAND()*1000, 2)
    FROM (
        SELECT @row := @row + 1 AS seq FROM
        (SELECT 0 UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4
         UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9) t1,
        (SELECT 0 UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4
         UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9) t2,
        (SELECT @row := 0) r
    ) numbers LIMIT 100;
-- 插入 10 条客户数据
INSERT INTO customer (name, phone)
    SELECT CONCAT('客户', seq), CONCAT('138', LPAD(seq, 8, '0'))
    FROM (
        SELECT @row2 := @row2 + 1 AS seq FROM
        (SELECT 0 UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4
         UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9) t1,
        (SELECT @row2 := 0) r
    ) numbers LIMIT 10;
SELECT 'orders.order_info' AS tbl, COUNT(*) AS cnt FROM order_info
UNION ALL
SELECT 'orders.customer', COUNT(*) FROM customer;
SQL

# ---------- 3. xtrabackup 备份 ----------
echo "3. 执行 xtrabackup 备份..."
mkdir -p "$BACKUP_DIR"
rm -rf "$BACKUP_DIR"/*
xtrabackup --backup \
    --user=root \
    --password="$MYSQL_ROOT_PWD" \
    --host=127.0.0.1 \
    --port=13307 \
    --target-dir="$BACKUP_DIR" \
    --no-lock

echo "   ✅ 备份完成：$BACKUP_DIR"

# ---------- 4. 清理临时容器 ----------
echo "4. 清理临时源容器..."
docker rm -f $MYSQL_CONTAINER
echo "   ✅ 已清理"

# ---------- 5. 汇总 ----------
echo ""
echo "===== 测试备份生成完成 ====="
echo "备份路径: $BACKUP_DIR"
echo "备份内容:"
ls -la "$BACKUP_DIR" | head -10
echo ""
echo "可将其加入 instances.yaml 用于 drill run 测试："
echo "  instances:"
echo "    - name: test-instance-1"
echo "      host: 127.0.0.1"
echo "      port: 13307"
echo "      mysql_version: \"8.0.35\""
echo "      backup_source_host: <本机IP>"
echo "      backup_source_path: /data/drill/test-backups/test-instance-1"
