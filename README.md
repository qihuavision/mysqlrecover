# mysqlrecover

mysql 的备份到底能不能恢复，别等出事了才知道。

解放双手 —— DBA 只需提供一台空服务器，自动完成装库、自动恢复、自动验证、日志留档。

管着上百个 mysql 实例（5.7、8.0 混着），备份虽然天天在做，但要确认备份真出事时恢复得出来，以前只能人肉：每台先装对应版本的 mysql，xtrabackup 恢复进去，再登录查一张表。一百台就循环一百遍，慢、容易漏，做完还留不下证据。

现在一条命令：

```bash
toolkit drill run --target <恢复机IP> --all --yes
```

工具自己认版本（配置里填错了也会从备份文件里纠正过来）、按版本分组并行恢复、起库、进库里数数、出报告、发企业微信。

## 常用命令

备份（接管原来 crontab 里的备份脚本，支持一次备全部实例）：

```bash
toolkit backup trigger --all --yes
```

恢复演练：

```bash
toolkit drill run --target <IP> --all --yes              # 全部
toolkit drill run --target <IP> --instance order-db --yes # 单个
toolkit drill run --target <IP> --resume 7 --yes          # 中途断了，从批次 7 续跑
```

体检和清理：

```bash
toolkit config doctor --target <IP>
toolkit cleanup --yes
```

## 版本支持

- 5.6 / 5.7 / 8.0 混合环境，版本白名单默认不限制，遇到新版本自动拉镜像
- 恢复前自动核对：备份是什么版本、恢复环境是什么版本、redo 格式是不是同一代，确定能不能恢复
- MySQL 8.0 改过两次 redo 格式（8.0.20 和 8.0.30，Percona 官方文档可查），工具据此分代：8.0.11~19、8.0.20~29、8.0.30+ 各用各的恢复环境，跨代直接拦截。同代内用最高版本恢复，不会降级；不同代并行跑
- 5.7 要用的 xtrabackup 2.4 和 8.0 的会冲突，直接走容器，宿主机不用装两份

## 验证规则

恢复起来只算一半，工具会登录进去：跳过系统库找业务库，挑有数据的表 count，数出来大于 0 才算过。

每台验证前还会先核对这个库的版本号对不对。这个闸是踩过坑加的——端口被别的容器占着，验证连到了别的库，报告显示成功但数据是错的。现在版本对不上直接判失败，不糊弄。

失败自动重试一次。xtrabackup 日志、验证日志、出错时的 docker 日志和 mysql 错误日志，都按日期归档在恢复机上，审计直接拿目录就行。

## 部署

恢复机（一台空服务器就行）：

- docker
- xtrabackup 8.0
- 用到的 mysql 版本镜像（docker pull）
- 跑一次 `scripts/bootstrap_recovery_host.sh`

管理机：

- python 3.10+（注意 CentOS 7 自带的 3.6 跑不了，见下面的老系统方案）
- ssh 免密到恢复机和备份机
- 两个环境变量：`DRILL_SSH_KEY`、`DRILL_MYSQL_PWD`

安装（**以下命令都在仓库根目录执行**，config/ 在根目录下，不在 src/ 里）：

```bash
git clone https://github.com/qihuavision/mysqlrecover.git
cd mysqlrecover
pip install -e .
cp config/config.example.yaml config/config.yaml
cp config/instances.example.yaml config/instances.yaml
# 改这两个文件
```

跑之前先 `toolkit config doctor --target <IP>` 检查一遍环境。

### 老系统（CentOS 7 等）怎么办

CentOS 7 的 yum 里没有 pip 这个包名，自带的 python 3.6 也太老。两个办法：

办法一，用 docker 跑工具，不碰 python：

```bash
# 装 docker 后，在仓库根目录：
docker build -t mysqlrecover:latest .

# 之后所有 toolkit 命令换成：
docker run --rm \
  -v $PWD/config:/app/config:ro \
  -v $PWD/data:/app/data \
  -v $PWD/logs:/app/logs \
  -v ~/.ssh:/app/ssh:ro \
  -e DRILL_MYSQL_PWD='密码' \
  mysqlrecover:latest <命令，如 drill run --target <IP> --all --yes>
```

办法二，miniconda 装个新 python：

```bash
curl -o /tmp/miniconda.sh https://mirrors.tuna.tsinghua.edu.cn/anaconda/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash /tmp/miniconda.sh -b -p /opt/miniconda3
/opt/miniconda3/bin/pip install -e .    # 在仓库根目录
```

## 定时

crontab 加三行，之后不用管了，周一早上看微信：

```cron
0 2 * * * toolkit backup trigger --all --yes
0 3 * * 0 toolkit drill run --target <IP> --all --yes
0 4 * * 0 toolkit cleanup --yes
```

## 目录

```
src/toolkit/   代码
docs/          立项书、PRD、设计、使用手册、交接文档
scripts/       恢复机初始化、造测试备份的脚本
tests/         单元测试
```

写得最全的是 `docs/11-使用手册.md`，每个命令带参数说明和例子。设计上的取舍都记在 `docs/99-交接文档.md`。

不做的事：监控告警（有 Prometheus）、主从切换（有 Orchestrator）、数据同步（有 Canal）。工具全程只读账号，不碰生产库，演练都在隔离的恢复机上做。
