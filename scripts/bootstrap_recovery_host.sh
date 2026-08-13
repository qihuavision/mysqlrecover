#!/bin/bash
# ============================================================
# 恢复机 bootstrap 脚本（Sprint 1 联调用）
# 在恢复机上执行一次，安装 Docker + xtrabackup + 创建目录
# 对应 ADR-09 前置依赖
# ============================================================
set -e

echo "===== 恢复机 bootstrap 开始 ====="

# ---------- 1. 检测 OS ----------
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS_ID=$ID
    OS_VERSION=$VERSION_ID
    echo "检测到系统: $OS_ID $OS_VERSION"
else
    echo "❌ 无法检测操作系统，仅支持 CentOS/RHEL/Ubuntu/Debian"
    exit 1
fi

# ---------- 2. 安装 Docker ----------
echo ""
echo "===== 安装 Docker ====="
if command -v docker &> /dev/null; then
    echo "✅ Docker 已安装: $(docker --version)"
else
    echo "安装 Docker..."
    if [[ "$OS_ID" == "centos" || "$OS_ID" == "rhel" || "$OS_ID" == "rocky" || "$OS_ID" == "almalinux" ]]; then
        yum install -y yum-utils
        yum-config-manager --add-repo https://mirrors.aliyun.com/docker-ce/linux/centos/docker-ce.repo
        sed -i 's|https://download.docker.com|https://mirrors.aliyun.com/docker-ce|g' /etc/yum.repos.d/docker-ce.repo
        yum install -y docker-ce docker-ce-cli containerd.io
    elif [[ "$OS_ID" == "ubuntu" || "$OS_ID" == "debian" ]]; then
        apt-get update
        apt-get install -y ca-certificates curl gnupg
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://mirrors.aliyun.com/docker-ce/linux/$OS_ID/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        chmod a+r /etc/apt/keyrings/docker.gpg
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://mirrors.aliyun.com/docker-ce/linux/$OS_ID $(. /etc/os-release && echo $VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list
        apt-get update
        apt-get install -y docker-ce docker-ce-cli containerd.io
    else
        echo "❌ 不支持的系统: $OS_ID，请手动安装 Docker"
        exit 1
    fi
    systemctl start docker
    systemctl enable docker
    echo "✅ Docker 安装完成: $(docker --version)"
fi

# ---------- 3. 配置 Docker 镜像加速（国内） ----------
echo ""
echo "===== 配置 Docker 镜像加速 ====="
mkdir -p /etc/docker
if [ ! -f /etc/docker/daemon.json ] || ! grep -q "registry-mirrors" /etc/docker/daemon.json; then
    cat > /etc/docker/daemon.json <<'EOF'
{
    "registry-mirrors": [
        "https://docker.1ms.run",
        "https://docker.xuanyuan.me"
    ]
}
EOF
    systemctl restart docker
    echo "✅ 镜像加速已配置"
else
    echo "✅ 镜像加速已存在，跳过"
fi

# ---------- 4. 安装 xtrabackup ----------
echo ""
echo "===== 安装 xtrabackup ====="
if command -v xtrabackup &> /dev/null; then
    echo "✅ xtrabackup 已安装: $(xtrabackup --version 2>&1 | head -1)"
else
    echo "安装 xtrabackup 8.0（MySQL 8.x 备份恢复用）..."
    if [[ "$OS_ID" == "centos" || "$OS_ID" == "rhel" || "$OS_ID" == "rocky" || "$OS_ID" == "almalinux" ]]; then
        yum install -y https://repo.percona.com/yum/percona-release-latest.noarch.rpm
        percona-release enable-only tools release
        yum install -y percona-xtrabackup-80
    elif [[ "$OS_ID" == "ubuntu" || "$OS_ID" == "debian" ]]; then
        wget -q https://repo.percona.com/apt/percona-release_latest.$(lsb_release -sc)_all.deb -O /tmp/percona-release.deb
        dpkg -i /tmp/percona-release.deb
        percona-release enable-only tools release
        apt-get update
        apt-get install -y percona-xtrabackup-80
    fi
    echo "✅ xtrabackup 安装完成: $(xtrabackup --version 2>&1 | head -1)"
fi

# ---------- 5. 创建数据目录 ----------
echo ""
echo "===== 创建数据目录 ====="
mkdir -p /data/drill/tmp-backups    # datadir 挂载根 + 临时备份
mkdir -p /data/archive               # 审计日志归档
mkdir -p /data/logs                  # 工具日志
chmod -R 755 /data
echo "✅ 目录创建完成:"
echo "   /data/drill/        - 容器 datadir 挂载根"
echo "   /data/archive/      - 审计日志归档"
echo "   /data/logs/         - 工具日志"

# ---------- 6. 安装 Python 3 ----------
echo ""
echo "===== 检查 Python ====="
if command -v python3 &> /dev/null; then
    PY_VER=$(python3 --version 2>&1)
    echo "✅ $PY_VER"
else
    echo "⚠️ Python3 未安装，工具需要 Python 3.10+"
    if [[ "$OS_ID" == "centos" || "$OS_ID" == "rhel" ]]; then
        echo "   建议: yum install -y python3"
    elif [[ "$OS_ID" == "ubuntu" || "$OS_ID" == "debian" ]]; then
        echo "   建议: apt-get install -y python3 python3-pip"
    fi
fi

# ---------- 7. 汇总 ----------
echo ""
echo "===== bootstrap 完成 ====="
echo ""
echo "环境检查："
echo "  Docker:    $(docker --version 2>/dev/null || echo '❌ 未安装')"
echo "  xtrabackup: $(xtrabackup --version 2>&1 | head -1 || echo '❌ 未安装')"
echo "  Python:    $(python3 --version 2>/dev/null || echo '⚠️ 未安装')"
echo "  磁盘可用:  $(df -h /data 2>/dev/null | tail -1 | awk '{print $4" 可用"}')"
echo ""
echo "下一步："
echo "  1. 在管理机设置环境变量："
echo "     export DRILL_SSH_KEY=~/.ssh/id_rsa"
echo "     export DRILL_MYSQL_PWD='<恢复出的MySQL密码>'"
echo "  2. 在管理机运行体检："
echo "     toolkit config doctor --config config/config.yaml --target <本机IP>"
