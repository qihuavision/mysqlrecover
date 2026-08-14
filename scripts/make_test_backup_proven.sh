#!/bin/bash
# 生成测试备份 v6（关闭 binlog 避免拷贝 binlog 失败）
set -e

BACKUP_DIR="/data/drill/test-backups/test-instance-1"
SOURCE_DATADIR="/data/drill/test-source-datadir"
MYSQL_CONTAINER="test-source-mysql"
MYSQL_ROOT_PWD="Test@123456"

echo "===== 生成测试备份 v6 ====="

echo "1. 启动 MySQL（network host + datadir 挂载 + 关闭binlog）..."
docker rm -f $MYSQL_CONTAINER 2>/dev/null || true
rm -rf "$SOURCE_DATADIR"
mkdir -p "$SOURCE_DATADIR"

docker run -d --network host --name $MYSQL_CONTAINER \
    -e MYSQL_ROOT_PASSWORD=$MYSQL_ROOT_PWD \
    -v "$SOURCE_DATADIR:/var/lib/mysql" \
    mysql:8.0.35 \
    --port=3306 \
    --mysqlx=0 \
    --skip-log-bin \
    --default-authentication-plugin=mysql_native_password

echo "   等待初始化（40秒）..."
sleep 40

if docker exec $MYSQL_CONTAINER mysql -uroot -p"$MYSQL_ROOT_PWD" -h127.0.0.1 -e "SELECT VERSION()" 2>/dev/null; then
    echo "   ✅ MySQL 就绪"
else
    echo "   ❌ 未就绪"
    docker logs $MYSQL_CONTAINER 2>&1 | tail -15
    exit 1
fi

echo "2. 创建测试数据..."
docker exec $MYSQL_CONTAINER mysql -uroot -p"$MYSQL_ROOT_PWD" -h127.0.0.1 -e "
CREATE DATABASE orders;
USE orders;
CREATE TABLE order_info (id BIGINT AUTO_INCREMENT PRIMARY KEY, order_no VARCHAR(32), amount DECIMAL(10,2));
CREATE TABLE customer (id BIGINT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(64), phone VARCHAR(20));
INSERT INTO order_info (order_no, amount) SELECT CONCAT('ORD', LPAD(seq, 8, '0')), ROUND(RAND()*1000, 2) FROM (SELECT a.N + b.N*10 + c.N*100 AS seq FROM (SELECT 0 N UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9) a, (SELECT 0 N UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9) b, (SELECT 0 N UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9) c) n LIMIT 100;
INSERT INTO customer (name, phone) SELECT CONCAT('客户', seq), CONCAT('138', LPAD(seq, 8, '0')) FROM (SELECT a.N + b.N*10 AS seq FROM (SELECT 0 N UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9) a, (SELECT 0 N UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9) b) n LIMIT 10;
SELECT 'order_info' t, COUNT(*) cnt FROM order_info UNION ALL SELECT 'customer', COUNT(*) FROM customer;
" 2>&1

echo "3. xtrabackup 备份..."
mkdir -p "$BACKUP_DIR"
rm -rf "$BACKUP_DIR"/*

xtrabackup --backup \
    --datadir="$SOURCE_DATADIR" \
    --target-dir="$BACKUP_DIR" \
    --user=root \
    --password="$MYSQL_ROOT_PWD" \
    --host=127.0.0.1 \
    --port=3306 2>&1 | tail -5

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

echo "5. 清理容器..."
docker rm -f $MYSQL_CONTAINER
echo ""
echo "===== 完成: $BACKUP_DIR ====="
