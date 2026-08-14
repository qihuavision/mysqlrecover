#!/bin/bash
# 生成第二个测试备份（test-instance-2，不同数据量，验证多实例编排）
set -e

BACKUP_DIR="/data/drill/test-backups/test-instance-2"
SOURCE_DATADIR="/data/drill/test-source2-datadir"
MYSQL_CONTAINER="test-source2-mysql"
MYSQL_ROOT_PWD="Test@123456"

echo "===== 生成第二个测试备份 ====="

docker rm -f $MYSQL_CONTAINER 2>/dev/null || true
rm -rf "$SOURCE_DATADIR"
mkdir -p "$SOURCE_DATADIR"

docker run -d --network host --name $MYSQL_CONTAINER \
    -e MYSQL_ROOT_PASSWORD=$MYSQL_ROOT_PWD \
    -v "$SOURCE_DATADIR:/var/lib/mysql" \
    mysql:8.0.35 \
    --port=3306 --mysqlx=0 --skip-log-bin \
    --default-authentication-plugin=mysql_native_password

echo "等待初始化（40秒）..."
sleep 40

if ! docker exec $MYSQL_CONTAINER mysql -uroot -p"$MYSQL_ROOT_PWD" -h127.0.0.1 -e "SELECT 1" &>/dev/null; then
    echo "❌ MySQL 未就绪"
    docker logs $MYSQL_CONTAINER 2>&1 | tail -15
    exit 1
fi
echo "✅ MySQL 就绪"

# 建不同的业务库（inventory，3张表 → 表数比 orders 多，验证不同实例恢复各自的数据）
docker exec $MYSQL_CONTAINER mysql -uroot -p"$MYSQL_ROOT_PWD" -h127.0.0.1 -e "
CREATE DATABASE inventory;
USE inventory;
CREATE TABLE products (id BIGINT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(64), price DECIMAL(10,2));
CREATE TABLE warehouse (id BIGINT AUTO_INCREMENT PRIMARY KEY, location VARCHAR(64), capacity INT);
CREATE TABLE stock_log (id BIGINT AUTO_INCREMENT PRIMARY KEY, product_id BIGINT, qty INT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
INSERT INTO products (name, price) SELECT CONCAT('商品', seq), ROUND(RAND()*500, 2) FROM (SELECT a.N + b.N*10 AS seq FROM (SELECT 0 N UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9) a, (SELECT 0 N UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9) b) n LIMIT 50;
INSERT INTO warehouse (location, capacity) SELECT CONCAT('仓库', seq), 1000*seq FROM (SELECT a.N + 1 AS seq FROM (SELECT 0 N UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4) a) n LIMIT 5;
INSERT INTO stock_log (product_id, qty) SELECT 1, 10;
SELECT 'products' t, COUNT(*) c FROM products UNION ALL SELECT 'warehouse', COUNT(*) FROM warehouse UNION ALL SELECT 'stock_log', COUNT(*) FROM stock_log;
" 2>&1
echo "✅ 数据创建完成（inventory: 3张表）"

mkdir -p "$BACKUP_DIR"
rm -rf "$BACKUP_DIR"/*

xtrabackup --backup \
    --datadir="$SOURCE_DATADIR" \
    --target-dir="$BACKUP_DIR" \
    --user=root --password="$MYSQL_ROOT_PWD" \
    --host=127.0.0.1 --port=3306 2>&1 | tail -3

if [ -f "$BACKUP_DIR/xtrabackup_checkpoints" ]; then
    echo "✅ 备份完整: $BACKUP_DIR ($(du -sh $BACKUP_DIR | awk '{print $1}'))"
else
    echo "❌ 备份失败"
    exit 1
fi

docker rm -f $MYSQL_CONTAINER
echo "===== 完成 ====="
