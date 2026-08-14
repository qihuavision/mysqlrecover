"""CLI 入口（Sprint 1 T9）。

基于 click。命令分组：mysql / backup / drill / instance / verify / config。
所有有副作用命令默认 --dry-run，需 --yes 才真正执行（安全设计）。
"""
from __future__ import annotations

import os
import sys

import click

from toolkit.core.config import Config, InstanceConfig, load_instances
from toolkit.core.db import init_db, get_session_ctx
from toolkit.core.logger import setup_logging, get_logger

CONFIG_PATH_OPTION = click.option(
    "--config", "config_path", default="config/config.yaml", help="全局配置文件路径"
)
INSTANCES_PATH_OPTION = click.option(
    "--instances", "instances_path", default="config/instances.yaml", help="实例清单文件路径"
)


def _bootstrap(config_path: str, verbose: bool = False):
    """加载配置 + 初始化日志 + 初始化数据库。"""
    cfg = Config.load(config_path)
    setup_logging(
        level="DEBUG" if verbose else cfg.logging.level,
        log_file=cfg.logging.file,
    )
    init_db(cfg.database.url)
    return cfg


@click.group()
@click.version_option(version="0.1.0")
@click.option("-v", "--verbose", is_flag=True, help="DEBUG 级别日志")
def main(verbose: bool) -> None:
    """MySQL 自动化备份与恢复演练工具。"""
    pass


# ==================== mysql 容器管理（FP-01）====================


@main.group()
def mysql() -> None:
    """MySQL 常驻容器管理（FP-01, ADR-09）。"""
    pass


@mysql.command("ensure")
@CONFIG_PATH_OPTION
@click.option("--version", required=True, help="MySQL 版本，如 8.0.35")
@click.option("--target", required=True, help="恢复目标机 IP")
@click.option("--yes", is_flag=True, help="跳过 dry-run，真正执行")
def mysql_ensure(config_path, version, target, yes):
    """确保某版本常驻容器存在（不存在则创建）。"""
    from toolkit.core.executor import SSHExecutor
    from toolkit.installer.docker import DockerInstaller

    cfg = _bootstrap(config_path)
    ssh_key = cfg.target.ssh_key_path
    executor = SSHExecutor(host=target, user=cfg.target.ssh_user,
                           port=cfg.target.ssh_port, key_path=ssh_key)
    installer = DockerInstaller(
        executor=executor,
        drill_port=cfg.docker.drill_port,
        datadir_base=cfg.docker.datadir_base,
        supported_versions=cfg.docker.supported_versions,
    )
    name = installer.ensure_container(version, dry_run=not yes)
    click.echo(f"容器 {name} 就绪 (dry_run={not yes})")


@mysql.command("list")
@CONFIG_PATH_OPTION
@click.option("--target", required=True, help="恢复目标机 IP")
def mysql_list(config_path, target):
    """列出常驻容器池。"""
    from toolkit.core.executor import SSHExecutor
    from toolkit.installer.docker import DockerInstaller

    cfg = _bootstrap(config_path)
    executor = SSHExecutor(host=target, user=cfg.target.ssh_user,
                           port=cfg.target.ssh_port, key_path=cfg.target.ssh_key_path)
    installer = DockerInstaller(executor=executor, drill_port=cfg.docker.drill_port,
                                datadir_base=cfg.docker.datadir_base)
    containers = installer.list_containers()
    if not containers:
        click.echo("无常驻容器")
        return
    click.echo(f"{'CONTAINER':<30} {'STATUS':<10}")
    for c in containers:
        click.echo(f"{c.name:<30} {c.status:<10}")


# ==================== backup 备份管理（FP-02）====================


@main.group()
def backup() -> None:
    """备份管理（FP-02）。"""
    pass


@backup.command("scan")
@CONFIG_PATH_OPTION
@INSTANCES_PATH_OPTION
@click.option("--instance", help="只扫描指定实例")
@click.option("--yes", is_flag=True, help="跳过 dry-run")
def backup_scan(config_path, instances_path, instance, yes):
    """扫描备份源机，登记可用备份。"""
    cfg = _bootstrap(config_path)
    instances = load_instances(instances_path)
    if instance:
        instances = [i for i in instances if i.name == instance]

    from toolkit.core.executor import SSHExecutor
    from toolkit.backup.locator import BackupLocator
    from toolkit.core.models import Instance as InstanceModel, Backup as BackupModel

    # 备份源机 executor
    ssh_key = cfg.backup.source_ssh_key_path
    source_executor = SSHExecutor(
        host=cfg.backup.source_host,
        user=cfg.backup.source_ssh_user,
        key_path=ssh_key,
    )
    locator = BackupLocator(source_executor)

    session = get_session_ctx()
    try:
        for inst in instances:
            latest = locator.find_latest(inst.backup_source_path)
            status = "available" if latest else "missing"
            click.echo(f"  {inst.name}: {status} {latest or ''}")
            if latest and yes:
                # 登记/更新到数据库
                db_inst = session.query(InstanceModel).filter_by(name=inst.name).first()
                if not db_inst:
                    db_inst = InstanceModel(
                        name=inst.name, host=inst.host, port=inst.port,
                        mysql_version=inst.mysql_version,
                        backup_source_host=inst.backup_source_host,
                        backup_source_path=inst.backup_source_path,
                        enabled=int(inst.enabled),
                    )
                    session.add(db_inst)
                    session.commit()
                record = BackupModel(
                    instance_id=db_inst.id, backup_path=latest,
                    size_bytes=locator.get_backup_size(latest),
                )
                session.add(record)
        if yes:
            session.commit()
            click.echo("已登记到数据库")
    finally:
        session.close()


@backup.command("list")
@CONFIG_PATH_OPTION
def backup_list(config_path):
    """列出已登记备份。"""
    _bootstrap(config_path)
    from toolkit.core.models import Backup, Instance
    session = get_session_ctx()
    try:
        backups = session.query(Backup).all()
        if not backups:
            click.echo("无已登记备份（先运行 backup scan）")
            return
        click.echo(f"{'ID':<5} {'INSTANCE':<25} {'STATUS':<12} {'PATH'}")
        for b in backups:
            inst = session.query(Instance).filter_by(id=b.instance_id).first()
            click.echo(f"{b.id:<5} {(inst.name if inst else '?'):<25} {b.status:<12} {b.backup_path}")
    finally:
        session.close()


@backup.command("trigger")
@CONFIG_PATH_OPTION
@INSTANCES_PATH_OPTION
@click.option("--instance", required=True, help="要备份的实例名")
@click.option("--backup-root", default="/data/backups", help="备份落盘根目录（源机本地）")
@click.option("--yes", is_flag=True, help="跳过 dry-run，真正执行")
def backup_trigger(config_path, instances_path, instance, backup_root, yes):
    """触发一次 xtrabackup 备份（FP-02, ADR-06 接管模式）。

    SSH 到实例所在机器执行 xtrabackup --backup，产物登记入库。
    用于替代原 crontab 备份任务。
    """
    cfg = _bootstrap(config_path)
    instances = load_instances(instances_path)
    target = [i for i in instances if i.name == instance]
    if not target:
        click.echo(f"❌ 实例 {instance} 不在清单中")
        sys.exit(1)
    inst_cfg = target[0]

    from toolkit.core.executor import SSHExecutor
    from toolkit.backup.runner import BackupRunner
    from toolkit.core.models import Instance as InstanceModel

    # 实例入库拿 ORM 对象
    db_instances = _sync_instances_to_db([inst_cfg])
    db_inst = db_instances[0]

    if not yes:
        click.echo(f"[dry-run] 将备份 {inst_cfg.name}（{inst_cfg.host}）-> {backup_root}/{inst_cfg.name}/")
        click.echo("加 --yes 真正执行")
        return

    # SSH 到实例所在机器
    ssh_key = cfg.target.ssh_key_path
    executor = SSHExecutor(host=inst_cfg.host, user=cfg.target.ssh_user,
                           port=cfg.target.ssh_port, key_path=ssh_key)
    runner = BackupRunner(
        executor=executor,
        xtrabackup_path=cfg.xtrabackup.binary_path,
        mysql_port=inst_cfg.port,
        # 容器化 MySQL：datadir 指宿主机挂载路径（物理机 MySQL 留空自动探测）
        datadir=f"{cfg.docker.datadir_base}/{inst_cfg.mysql_version}/datadir"
                if inst_cfg.port == cfg.docker.drill_port else "",
    )
    try:
        backup_id = runner.run_backup(db_inst, backup_root)
        click.echo(f"✅ 备份完成并登记：backup #{backup_id}")
    except Exception as e:
        click.echo(f"❌ 备份失败: {e}")
        sys.exit(1)


# ==================== drill 恢复演练（FP-03 核心）====================


@main.group()
def drill() -> None:
    """恢复演练（FP-03, ADR-09）。"""
    pass


@drill.command("run")
@CONFIG_PATH_OPTION
@INSTANCES_PATH_OPTION
@click.option("--target", required=True, help="恢复目标机 IP")
@click.option("--all", "all_instances", is_flag=True, help="演练所有 enabled 实例")
@click.option("--instance", help="指定单个实例名")
@click.option("--yes", is_flag=True, help="跳过 dry-run，真正执行")
@click.option("--resume", "resume_run_id", type=int, default=None,
              help="断点续跑：从指定批次 ID 继续（配合 --yes）")
def drill_run(config_path, instances_path, target, all_instances, instance, yes, resume_run_id):
    """执行恢复演练（多实例编排 + 失败重试 + 断点续跑）。默认 dry-run。"""
    cfg = _bootstrap(config_path)

    # 构建组件
    from toolkit.core.executor import SSHExecutor
    from toolkit.installer.docker import DockerInstaller
    from toolkit.backup.xtrabackup import Xtrabackup
    from toolkit.recovery.verifier import Verifier
    from toolkit.recovery.task import RecoveryTaskRunner
    from toolkit.recovery.orchestrator import Orchestrator

    ssh_key = cfg.target.ssh_key_path
    executor = SSHExecutor(host=target, user=cfg.target.ssh_user,
                           port=cfg.target.ssh_port, key_path=ssh_key)
    installer = DockerInstaller(
        executor=executor, drill_port=cfg.docker.drill_port,
        datadir_base=cfg.docker.datadir_base,
        supported_versions=cfg.docker.supported_versions,
    )
    xb = Xtrabackup(executor=executor, binary_path=cfg.xtrabackup.binary_path)
    verifier = Verifier(host=target, port=cfg.docker.drill_port)
    runner = RecoveryTaskRunner(
        executor=executor, installer=installer, xtrabackup=xb,
        verifier=verifier, archive_root=cfg.archive.root,
        tmp_backup_dir=cfg.target.tmp_backup_dir,
    )

    # Sprint 3：报告 + 通知
    from toolkit.reporters.markdown import MarkdownReporter
    from toolkit.notifiers.wecom import WeComNotifier
    reporter = MarkdownReporter(output_dir=cfg.report.output_dir)
    notifier = None
    if cfg.notifiers.wecom.enabled:
        notifier = WeComNotifier(
            webhook_env=cfg.notifiers.wecom.webhook_env,
            notify_on=cfg.notifiers.wecom.notify_on,
        )

    orch = Orchestrator(
        task_runner=runner, max_retry=cfg.drill.max_retry,
        reporter=reporter, notifier=notifier,
        archive_root=cfg.archive.root,
    )

    # 断点续跑模式
    if resume_run_id:
        if not yes:
            click.echo("断点续跑需要 --yes")
            sys.exit(1)
        click.echo(f">>> 断点续跑批次 #{resume_run_id}...")
        result = orch.run(target_host=target, instances=[], dry_run=False,
                          resume_run_id=resume_run_id)
        _print_drill_result(result)
        return

    # 正常模式：筛选实例
    instances = load_instances(instances_path)
    if instance:
        instances = [i for i in instances if i.name == instance]
    elif all_instances:
        instances = [i for i in instances if i.enabled]
    else:
        click.echo("请指定 --all 或 --instance <name>")
        sys.exit(1)

    # 实例同步入库（编排器需要 ORM Instance）
    db_instances = _sync_instances_to_db(instances)

    click.echo(f"待演练实例: {len(db_instances)} 个 (dry_run={not yes})")

    result = orch.run(target_host=target, instances=db_instances, dry_run=not yes)
    _print_drill_result(result)


def _sync_instances_to_db(instances_cfg) -> list:
    """把 YAML 实例配置同步到数据库（存在则更新），返回 ORM Instance 列表。"""
    from toolkit.core.models import Instance as InstanceModel
    session = get_session_ctx()
    try:
        result = []
        for cfg_inst in instances_cfg:
            db_inst = session.query(InstanceModel).filter_by(name=cfg_inst.name).first()
            if db_inst:
                # 更新
                db_inst.host = cfg_inst.host
                db_inst.port = cfg_inst.port
                db_inst.mysql_version = cfg_inst.mysql_version
                db_inst.backup_source_host = cfg_inst.backup_source_host
                db_inst.backup_source_path = cfg_inst.backup_source_path
                db_inst.enabled = int(cfg_inst.enabled)
            else:
                db_inst = InstanceModel(
                    name=cfg_inst.name, host=cfg_inst.host, port=cfg_inst.port,
                    mysql_version=cfg_inst.mysql_version,
                    backup_source_host=cfg_inst.backup_source_host,
                    backup_source_path=cfg_inst.backup_source_path,
                    enabled=int(cfg_inst.enabled),
                )
                session.add(db_inst)
            result.append(db_inst)
        session.commit()
        return result
    finally:
        session.close()


def _print_drill_result(result) -> None:
    """打印演练结果汇总。"""
    click.echo(f"\n===== 演练完成 =====")
    click.echo(f"批次: #{result.run_id}")
    click.echo(f"总计: {result.total}  成功: {result.success}  失败: {result.failed}  "
               f"重试: {result.retried}  跳过: {result.skipped}")
    click.echo(f"耗时: {result.duration_sec}s")
    if result.task_results:
        click.echo("\n明细:")
        for t in result.task_results:
            status_icon = {"SUCCESS": "✅", "FAILED_FINAL": "❌", "DRY_RUN": "📝"}.get(
                t.get("status", ""), "⏳"
            )
            click.echo(f"  {status_icon} {t.get('instance', '?')} (v{t.get('version', '?')}) "
                       f"[{t.get('status')}] attempt={t.get('attempt', 1)} "
                       f"{t.get('duration_sec', 0)}s")
            if t.get("error"):
                click.echo(f"     错误: {t['error']}")


@drill.command("status")
@CONFIG_PATH_OPTION
def drill_status(config_path):
    """查看演练进度（Sprint 2 完善）。"""
    _bootstrap(config_path)
    from toolkit.core.models import DrillRun
    session = get_session_ctx()
    try:
        run = session.query(DrillRun).filter_by(status="running").first()
        if not run:
            click.echo("无进行中的演练")
            return
        click.echo(f"Run #{run.id} target={run.target_host} {run.status}")
        click.echo(f"进度: {run.success_count + run.failed_count}/{run.total_count}")
    finally:
        session.close()


# ==================== cleanup 清理（Sprint 4）====================


@main.command("cleanup")
@CONFIG_PATH_OPTION
@click.option("--target", help="恢复机 IP（清理远程临时备份需要）")
@click.option("--tasks-days", default=180, show_default=True, help="任务/日志保留天数")
@click.option("--reports-days", default=90, show_default=True, help="报告文件保留天数")
@click.option("--backups-days", default=7, show_default=True, help="远程临时备份保留天数")
@click.option("--yes", is_flag=True, help="真正执行（默认 dry-run）")
def cleanup(config_path, target, tasks_days, reports_days, backups_days, yes):
    """清理超期数据：旧任务记录 + 旧报告 + 远程临时备份。"""
    cfg = _bootstrap(config_path)
    from toolkit.core import retention

    if not yes:
        click.echo(f"[dry-run] 将清理：")
        click.echo(f"  - 超过 {tasks_days} 天的任务/日志记录（数据库）")
        click.echo(f"  - 超过 {reports_days} 天的报告文件（{cfg.report.output_dir}）")
        if target:
            click.echo(f"  - 超过 {backups_days} 天的临时备份（恢复机 {target}）")
        click.echo("加 --yes 真正执行")
        return

    total = 0
    # 1. 数据库旧任务
    n1 = retention.cleanup_old_tasks(keep_days=tasks_days)
    click.echo(f"✅ 清理数据库旧任务: {n1} 条")

    # 2. 本地旧报告
    n2 = retention.cleanup_local_reports(cfg.report.output_dir, keep_days=reports_days)
    click.echo(f"✅ 清理旧报告文件: {n2} 个")

    # 3. 远程临时备份
    if target:
        from toolkit.core.executor import SSHExecutor
        executor = SSHExecutor(host=target, user=cfg.target.ssh_user,
                               port=cfg.target.ssh_port, key_path=cfg.target.ssh_key_path)
        n3 = retention.cleanup_remote_backups(
            executor, cfg.target.tmp_backup_dir, keep_days=backups_days
        )
        click.echo(f"✅ 清理远程临时备份: {n3} 个目录")
        total += n3

    total += n1 + n2
    click.echo(f"\n===== 清理完成，共 {total} 项 =====")


# ==================== config 配置管理 ====================


@main.group()
def config_group() -> None:
    """配置管理。"""
    pass


# click 里 "config" 和内置冲突，用 config_group + alias
main.add_command(config_group, name="config")


@config_group.command("check")
@CONFIG_PATH_OPTION
@INSTANCES_PATH_OPTION
def config_check(config_path, instances_path):
    """检查配置 + 环境变量是否就绪。"""
    click.echo("=== 配置检查 ===")
    # 全局配置
    try:
        cfg = Config.load(config_path)
        click.echo(f"✅ 全局配置加载成功: {config_path}")
    except Exception as e:
        click.echo(f"❌ 全局配置加载失败: {e}")
        sys.exit(1)

    # 实例清单
    try:
        instances = load_instances(instances_path)
        click.echo(f"✅ 实例清单加载成功: {len(instances)} 个实例")
    except Exception as e:
        click.echo(f"❌ 实例清单加载失败: {e}")
        sys.exit(1)

    # 环境变量
    click.echo("\n=== 环境变量 ===")
    mysql_pwd = os.environ.get("DRILL_MYSQL_PWD")
    click.echo(f"  DRILL_MYSQL_PWD: {'✅ 已设置' if mysql_pwd else '⚠️ 未设置'}")
    ssh_key = os.environ.get("DRILL_SSH_KEY")
    click.echo(f"  DRILL_SSH_KEY: {'✅ ' + ssh_key if ssh_key else '⚠️ 未设置'}")

    # 版本白名单
    click.echo(f"\n=== 支持版本 ===\n  {cfg.docker.supported_versions}")


@config_group.command("doctor")
@CONFIG_PATH_OPTION
@click.option("--target", help="恢复目标机 IP（检查 SSH/Docker/xtrabackup）")
def config_doctor(config_path, target):
    """环境体检。"""
    cfg = Config.load(config_path)
    setup_logging(level=cfg.logging.level)
    click.echo("=== 环境体检 ===")

    if target:
        from toolkit.core.executor import SSHExecutor
        ssh_key = cfg.target.ssh_key_path
        ex = SSHExecutor(host=target, user=cfg.target.ssh_user,
                         port=cfg.target.ssh_port, key_path=ssh_key)
        # SSH
        res = ex.run("echo ok")
        click.echo(f"  SSH {target}: {'✅' if res.ok else '❌'}")

        # Docker
        res = ex.run("docker --version")
        click.echo(f"  Docker: {'✅ ' + res.stdout.strip() if res.ok else '❌ 未安装'}")

        # xtrabackup
        res = ex.run(f"{cfg.xtrabackup.binary_path} --version")
        click.echo(f"  xtrabackup: {'✅' if res.ok else '❌ 未安装'}")

        # datadir 目录
        res = ex.run(f"test -d {cfg.docker.datadir_base} && echo yes || echo no")
        click.echo(f"  {cfg.docker.datadir_base}: {'✅' if 'yes' in res.stdout else '⚠️ 不存在'}")


if __name__ == "__main__":
    main()
