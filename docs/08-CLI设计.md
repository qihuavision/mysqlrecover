# CLI 命令设计

> 工具入口：`python -m toolkit` 或安装后直接 `toolkit`。
> 基于 `click` 库。所有有副作用的命令默认 `--dry-run`，需 `--yes` 才真正执行（安全设计）。
> 最后更新：2026-08-13（适配 ADR-08/09/10）

---

## 命令总览

```
toolkit
├── mysql       MySQL 常驻容器管理（FP-01, ADR-09）
│   ├── ensure      确保某版本容器存在（不存在则创建）
│   ├── list        列出常驻容器池
│   ├── start       启动某版本容器
│   ├── stop        停止某版本容器
│   └── logs        查看容器日志
├── backup       备份管理（FP-02, ADR-06）
│   ├── scan        扫描备份源机，登记可用备份
│   ├── list        列出已登记备份
│   └── trigger     触发一次备份（Sprint 3，替换 crontab）
├── drill        恢复演练（FP-03, ADR-09 核心）
│   ├── run         执行一次演练
│   ├── status      查看演练进度
│   ├── history     查看历史演练
│   └── report      生成/查看演练报告
├── instance     实例管理
│   ├── list        列出所有实例
│   ├── sync        从 instances.yaml 同步到数据库
│   └── enable/disable  启用/禁用某实例
├── verify       验证工具（FP-04, ADR-10）
│   └── test        对已运行的容器执行自动验证（调试用）
└── config       配置管理
    ├── check       检查配置 + 环境变量（密码等）是否就绪
    └── doctor      环境体检（Docker/xtrabackup/SSH 连通性）
```

---

## 核心命令详解

### `toolkit mysql ensure` —— 确保常驻容器存在（FP-01）

```bash
# 确保 8.0.35 版本容器存在（不存在则 pull + run）
toolkit mysql ensure --version 8.0.35 --target 10.0.1.50

# 完整参数
toolkit mysql ensure \
  --version 8.0.35 \
  --target 10.0.1.50 \           # 恢复机 IP
  --port 13306 \                 # 固定端口（默认 13306，ADR-09）
  --datadir /data/drill/8.0.35/datadir \  # 挂载目录
  --yes                          # 跳过 dry-run
```

**行为**：检查容器 `drill-mysql-8035` 是否存在 → 不存在则 `docker pull mysql:8.0.35` + `docker run -d --network host --name drill-mysql-8035 -v {datadir}:/var/lib/mysql mysql:8.0.35 --port=13306` → 记录到 `mysql_containers` 表。

---

### `toolkit mysql list` —— 列出常驻容器池

```bash
toolkit mysql list --target 10.0.1.50
```

**输出示例**：
```
CONTAINER             VERSION  STATUS   PORT   DATADIR
drill-mysql-8035      8.0.35   stopped  13306  /data/drill/8.0.35/datadir
drill-mysql-5744      5.7.44   stopped  13306  /data/drill/5.7.44/datadir
```

---

### `toolkit backup scan` —— 扫描备份源机（FP-02）

```bash
# 扫描所有实例的备份源机，登记可用备份
toolkit backup scan --yes

# 扫描指定实例
toolkit backup scan --instance order-db-prod-01 --yes
```

**行为**：SSH 到备份源机，扫描 `backup_source_path`，登记到 `backups` 表。

---

### `toolkit drill run` —— 执行恢复演练（🔥 核心，FP-03）

```bash
# 演练所有启用实例（最常用）
toolkit drill run --target 10.0.1.50 --all --yes

# 演练指定实例
toolkit drill run --target 10.0.1.50 --instance order-db-prod-01 --yes

# dry-run 模式（默认，只打印计划不执行）
toolkit drill run --target 10.0.1.50 --all

# 指定只演练某版本
toolkit drill run --target 10.0.1.50 --version 8.0.35 --yes

# 定时模式（写入 crontab）
toolkit drill run --target 10.0.1.50 --all --schedule "0 2 * * 0"  # 每周日 2 点
```

**参数**：
| 参数 | 说明 |
|---|---|
| `--target` | 恢复目标机 IP（必填） |
| `--all` | 演练所有 enabled 实例 |
| `--instance NAME` | 指定单个实例（可多次） |
| `--version VER` | 只演练某 MySQL 版本 |
| `--yes` | 跳过 dry-run 真正执行 |
| `--schedule CRON` | 定时执行（写入管理机 crontab） |

**行为**：按 ADR-09 的 12 步流程，串行恢复所有指定实例，自动验证（ADR-10），失败重试（FP-05），结束出报告（FP-06）+ 通知（FP-07）。

---

### `toolkit drill status` —— 查看演练进度

```bash
toolkit drill status                    # 查看当前进行中的批次
toolkit drill status --run-id 42        # 查看指定批次
```

**输出示例**：
```
Run #42  target=10.0.1.50  status=running  started=2026-08-13T14:30:00
Progress: 15/100  success=12  failed=2  retrying=1

CURRENT: order-db-prod-03 (8.0.35)  RUNNING  attempt=1
```

---

### `toolkit drill report` —— 生成/查看报告（FP-06）

```bash
toolkit drill report --run-id 42                    # 输出 Markdown 到 stdout
toolkit drill report --run-id 42 --output report.md # 保存到文件
toolkit drill report --last                          # 最近一次演练报告
```

---

### `toolkit verify test` —— 手动验证（调试用，FP-04）

```bash
# 对当前 running 的容器执行自动验证（调试用）
toolkit verify test --target 10.0.1.50 --port 13306
```

**行为**：连 13306，执行 ADR-10 自动发现验证，输出选中的库/表/COUNT 结果。用于恢复后手动确认。

---

### `toolkit config doctor` —— 环境体检

```bash
toolkit config doctor --target 10.0.1.50
```

**检查项**：
- ✅/❌ 管理机 → 恢复机 SSH 连通
- ✅/❌ 恢复机 Docker 可用
- ✅/❌ 恢复机 xtrabackup 可用
- ✅/❌ 恢复机 `/data/drill/` 目录存在
- ✅/❌ 管理机 → 备份源机 SSH 连通
- ✅/❌ 环境变量 `DRILL_MYSQL_PWD` 已设置
- ✅/❌ `instances.yaml` 配置合法

---

### `toolkit config check` —— 配置检查

```bash
toolkit config check
```

**检查**：YAML 语法、必填字段、环境变量密码、实例版本白名单。Sprint 1 首个要实现的命令（先确保配置正确再做事）。

---

## 设计原则

1. **默认安全**：所有有副作用的命令默认 `--dry-run`，必须 `--yes` 才执行
2. **幂等**：`ensure`/`scan` 等命令可重复执行，不会重复创建/登记
3. **可观测**：`status`/`list`/`doctor` 提供随时查看状态的能力
4. **目标机显式**：涉及恢复机的命令都要 `--target`（防止误操作错机器）
5. **子命令分组清晰**：mysql/backup/drill/instance/verify/config 各管一摊
