# ===== 工具自身镜像（Sprint 4 交付版）=====
# 定位：管理机容器 —— 跑 toolkit CLI，SSH 控制恢复机（ADR-01）
# 恢复机上的 Docker/xtrabackup 由 bootstrap_recovery_host.sh 准备，不在此镜像内
FROM python:3.11-slim

# 系统依赖：ssh client（连恢复机/备份源机）+ ca-certificates
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 toolkit

WORKDIR /app

# Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://mirrors.aliyun.com/pypi/simple/ || \
    pip install --no-cache-dir -r requirements.txt

# 源码 + 安装
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir -e .

# 配置/数据/SSH 密钥挂载点
# config:  config.yaml + instances.yaml
# data:    SQLite 元数据库
# logs:    工具日志 + 报告
# ssh:     SSH 私钥（DRILL_SSH_KEY 指向这里）
VOLUME ["/app/config", "/app/data", "/app/logs", "/app/ssh"]
ENV DRILL_SSH_KEY=/app/ssh/id_rsa

USER toolkit

ENTRYPOINT ["toolkit"]
CMD ["--help"]

# ===== 使用示例 =====
# 构建：
#   docker build -t mysqlrecover:latest .
#
# 运行（管理机上）：
#   docker run --rm \
#     -v $(pwd)/config:/app/config \
#     -v $(pwd)/data:/app/data \
#     -v $(pwd)/logs:/app/logs \
#     -v ~/.ssh:/app/ssh:ro \
#     -e DRILL_MYSQL_PWD='密码' \
#     mysqlrecover drill run --target 192.168.1.15 --all --yes
