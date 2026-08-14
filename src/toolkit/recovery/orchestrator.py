"""恢复编排主流程（FP-03 核心, Sprint 5 大改）。

Sprint 5 新能力：
1. 版本自动检测：从备份文件（xtrabackup_info）读出 MySQL 版本，覆盖手工配置
2. 备份拉取：恢复前把备份从源机拉到恢复机（ADR-04 scp / 同机 cp）
3. 端口分配：每版本独立端口（PortAllocator）
4. 多版本并行：按版本分组，每组一个线程串行执行，跨版本同时恢复

线程安全设计：每个版本线程用独立的 SSHExecutor/DockerInstaller/Xtrabackup/
Verifier/TaskRunner（由 runner_factory 按端口创建），互不共享连接。
"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from toolkit.core.db import get_session_ctx
from toolkit.core.logger import get_logger
from toolkit.core.models import Backup, DrillRun, Instance, RecoveryTask, TaskStatus
from toolkit.core.port_allocator import PortAllocator
from toolkit.recovery.queue import TaskQueue
from toolkit.recovery.task import RecoveryTaskRunner, TaskResult

logger = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class DrillResult:
    """一次演练的整体结果。"""

    run_id: int = 0
    target_host: str = ""
    total: int = 0
    success: int = 0
    failed: int = 0
    retried: int = 0
    skipped: int = 0
    duration_sec: int = 0
    version_ports: dict = field(default_factory=dict)  # 版本→端口（Sprint 5）
    task_results: list[dict] = field(default_factory=list)


@dataclass
class _PlannedTask:
    """编排计划（版本检测 + 拉取后）。"""

    instance: Instance
    backup_id: int
    local_backup_path: str   # 恢复机本地路径
    version: str             # 自动检测出的版本（覆盖配置）


class Orchestrator:
    """恢复演练编排引擎（Sprint 5：多版本并行 + 版本自动检测）。"""

    def __init__(
        self,
        task_runner: RecoveryTaskRunner,           # 主线程用（单版本/串行模式）
        runner_factory: Callable[[int], RecoveryTaskRunner] | None = None,
        # runner_factory(port) → 每版本线程独立的 TaskRunner（多版本并行必需）
        locator_factory: Callable[[], object] | None = None,  # 备份源机 locator 工厂（版本检测用）
        puller_factory: Callable[[], object] | None = None,   # 备份拉取器工厂
        max_retry: int = 1,
        parallel: bool = True,   # 多版本并行开关
        reporter=None,
        notifier=None,
        archive_root: str = "",
        recovery_host: str = "",  # 恢复机 IP（判断备份同机）
    ):
        self.task_runner = task_runner
        self.runner_factory = runner_factory
        self.locator_factory = locator_factory
        self.puller_factory = puller_factory
        self.max_retry = max_retry
        self.parallel = parallel
        self.reporter = reporter
        self.notifier = notifier
        self.archive_root = archive_root
        self.recovery_host = recovery_host

    # ---------- 主入口 ----------

    def run(
        self,
        target_host: str,
        instances: list[Instance],
        dry_run: bool = True,
        resume_run_id: int | None = None,
    ) -> DrillResult:
        """执行一次完整演练。

        流程（Sprint 5）：
        1. 创建批次
        2. 逐实例：版本自动检测 → 备份拉取到恢复机（计划阶段，串行 IO）
        3. 端口分配（版本→端口）
        4. 按版本分组：单版本串行；多版本且 parallel=True 时线程并行
        5. 聚合 + 报告 + 通知
        """
        start = datetime.now(timezone.utc)

        if dry_run:
            return self._dry_run(target_host, instances)

        run_id = (
            resume_run_id
            if resume_run_id
            else self._create_run(target_host, len(instances))
        )
        result = DrillResult(run_id=run_id, target_host=target_host, total=len(instances))

        # ---- 阶段 1：计划（版本检测 + 拉取，串行）----
        plans, skipped = self._plan(instances)
        result.skipped = skipped

        # ---- 阶段 2：端口分配 ----
        versions = sorted({p.version for p in plans})
        allocator = PortAllocator(base_port=self._base_port())
        ports = allocator.assign(versions)
        result.version_ports = ports
        logger.info("版本分组: %s → 端口 %s", versions, ports)

        # ---- 阶段 3：登记任务 ----
        tasks = self._register_tasks(run_id, plans)

        # ---- 阶段 4：执行（版本分组）----
        groups: dict[str, list] = defaultdict(list)
        for plan, task in zip(plans, tasks):
            groups[plan.version].append((plan, task))

        group_results: list[dict] = []
        if len(groups) == 1 or not self.parallel or not self.runner_factory:
            # 单版本 / 关闭并行：主线程串行跑所有组
            for version, items in groups.items():
                group_results.extend(
                    self._run_group(version, items, ports[version])
                )
        else:
            # 多版本并行：每版本一个线程
            with ThreadPoolExecutor(max_workers=len(groups)) as pool:
                futures = {
                    pool.submit(self._run_group, ver, items, ports[ver]): ver
                    for ver, items in groups.items()
                }
                for fut in as_completed(futures):
                    ver = futures[fut]
                    try:
                        group_results.extend(fut.result())
                        logger.info("版本组 %s 全部完成", ver)
                    except Exception as e:
                        logger.error("版本组 %s 异常: %s", ver, e, exc_info=True)

        # ---- 阶段 5：聚合 ----
        for tr in group_results:
            if tr.get("status") == "SUCCESS":
                result.success += 1
            else:
                result.failed += 1
            if tr.get("attempt", 1) > 1:
                result.retried += 1
        result.task_results = group_results
        result.duration_sec = int((datetime.now(timezone.utc) - start).total_seconds())

        self._finish_run(run_id, result)
        self._report_and_notify(result)
        return result

    # ---------- 阶段：计划（版本检测 + 拉取）----------

    def _plan(self, instances: list[Instance]) -> tuple[list[_PlannedTask], int]:
        """对每个实例：定位备份 → 自动检测版本 → 拉取到恢复机。"""
        plans: list[_PlannedTask] = []
        skipped = 0

        locator = self.locator_factory() if self.locator_factory else None
        puller = self.puller_factory() if self.puller_factory else None

        session = get_session_ctx()
        try:
            for inst in instances:
                # 1. 找最新可用备份（已登记 or 降级用配置路径）
                backup = (
                    session.query(Backup)
                    .filter_by(instance_id=inst.id, status="available")
                    .order_by(Backup.finished_at.desc())
                    .first()
                )
                if not backup and inst.backup_source_path:
                    backup = Backup(instance_id=inst.id, backup_path=inst.backup_source_path,
                                    status="available")
                    session.add(backup)
                    session.flush()
                if not backup:
                    skipped += 1
                    logger.warning("实例 %s 无可用备份，跳过", inst.name)
                    continue

                source_path = backup.backup_path

                # 2. 版本自动检测（源机上读 xtrabackup_info，Sprint 5）
                version = inst.mysql_version  # 配置兜底
                if locator is not None:
                    detected = locator.detect_mysql_version(source_path)
                    if detected:
                        if detected != inst.mysql_version:
                            logger.info(
                                "实例 %s 版本自动检测: %s（覆盖手工配置 %s）",
                                inst.name, detected, inst.mysql_version,
                            )
                        version = detected
                    else:
                        logger.warning("实例 %s 版本检测失败，用配置值 %s",
                                       inst.name, inst.mysql_version)

                # 3. 拉取备份到恢复机（源机≠恢复机时；同机 cp）
                local_path = source_path
                if puller is not None:
                    try:
                        local_path = puller.pull(
                            backup_source_path=source_path,
                            backup_source_host=inst.backup_source_host,
                            recovery_host=self.recovery_host or inst.backup_source_host,
                            instance_name=inst.name,
                        )
                    except Exception as e:
                        logger.error("实例 %s 备份拉取失败: %s", inst.name, e)
                        skipped += 1
                        continue

                # 4. 更新备份登记（记录检测出的路径）
                backup.finished_at = backup.finished_at or _now_iso()
                session.commit()

                plans.append(_PlannedTask(
                    instance=inst, backup_id=backup.id,
                    local_backup_path=local_path, version=version,
                ))
            return plans, skipped
        finally:
            session.close()

    # ---------- 阶段：执行一个版本组 ----------

    def _run_group(self, version: str, items: list, port: int) -> list[dict]:
        """执行一个版本组（组内串行 + 重试）。返回任务结果列表。"""
        runner = (
            self.runner_factory(port)
            if self.runner_factory
            else self.task_runner
        )
        queue = TaskQueue(max_retry=self.max_retry)
        results: list[dict] = []

        # 组内队列
        todo = list(items)
        retry: list = []

        while todo or retry:
            if todo:
                plan, task = todo.pop(0)
                attempt_start = 1
            else:
                plan, task = retry.pop(0)
                attempt_start = (task.attempt or 1) + 1
            task.attempt = attempt_start

            inst = plan.instance
            logger.info(">>> [%s] 开始演练 (v%s, 端口%d, 第%d次)",
                        inst.name, version, port, task.attempt)
            task.status = TaskStatus.RUNNING
            task_result = runner.execute(
                mysql_version=version,
                backup_remote_path=plan.local_backup_path,
                instance_name=inst.name,
                backup_source_host=inst.backup_source_host,
                dry_run=False,
                port=port,
            )
            # 填充任务字段
            task.duration_sec = task_result.duration_sec
            task.verify_db = task_result.verify_db
            task.verify_table = task_result.verify_table
            task.verify_count = task_result.verify_count
            task.container_name = task_result.container_name

            if task_result.success:
                task.status = TaskStatus.SUCCESS
                task.finished_at = _now_iso()
                logger.info("<<< [%s] ✅ 成功 (%ds)", inst.name, task_result.duration_sec)
            else:
                task.error_msg = task_result.error_msg
                if (task.attempt or 1) <= self.max_retry:
                    task.status = TaskStatus.RETRYING
                    retry.append((plan, task))
                    logger.warning("<<< [%s] ❌ 失败进重试: %s",
                                   inst.name, (task_result.error_msg or "")[:80])
                    if task.id:
                        queue.persist_task(task)
                    continue  # 重试中间态不计入结果

                task.status = TaskStatus.FAILED_FINAL
                task.finished_at = _now_iso()
                logger.warning("<<< [%s] ❌ 最终失败: %s", inst.name, task_result.error_msg)

            if task.id:
                queue.persist_task(task)
            # 只有终态（SUCCESS / FAILED_FINAL）计入结果
            results.append(self._task_to_dict(task, inst, version))

        return results

    # ---------- 辅助 ----------

    def _base_port(self) -> int:
        """基础端口（从 task_runner 的 installer 默认端口取）。"""
        installer = getattr(self.task_runner, "installer", None)
        return getattr(installer, "drill_port", 13306)

    def _create_run(self, target_host: str, total: int) -> int:
        session = get_session_ctx()
        try:
            run = DrillRun(target_host=target_host, total_count=total)
            session.add(run)
            session.commit()
            logger.info("创建演练批次 #%d（%d 个实例）", run.id, total)
            return run.id
        finally:
            session.close()

    def _register_tasks(self, run_id: int, plans: list[_PlannedTask]) -> list[RecoveryTask]:
        session = get_session_ctx()
        try:
            tasks = []
            for p in plans:
                task = RecoveryTask(
                    run_id=run_id, instance_id=p.instance.id, backup_id=p.backup_id,
                    container_name="", status=TaskStatus.PENDING,
                )
                session.add(task)
                tasks.append(task)
            session.commit()
            logger.info("已登记 %d 个任务到批次 #%d", len(tasks), run_id)
            return tasks
        finally:
            session.close()

    def _finish_run(self, run_id: int, result: DrillResult) -> None:
        session = get_session_ctx()
        try:
            run = session.query(DrillRun).filter_by(id=run_id).first()
            if run:
                run.success_count = result.success
                run.failed_count = result.failed
                run.retry_count = result.retried
                run.status = "completed"
                run.finished_at = _now_iso()
                run.duration_sec = result.duration_sec
                session.commit()
        finally:
            session.close()

    def _report_and_notify(self, result: DrillResult) -> None:
        """出报告 + 发通知。失败不阻塞主流程。"""
        report_path = ""
        if self.reporter:
            try:
                report_path = str(self.reporter.render(result, archive_root=self.archive_root))
            except Exception as e:
                logger.error("报告生成失败（不影响演练结果）: %s", e, exc_info=True)

        if self.notifier:
            try:
                if self.notifier.should_notify(result.failed):
                    summary = self.reporter.summarize_markdown(result) if self.reporter else ""
                    if report_path:
                        summary += f"\n\n📄 报告：`{report_path}`"
                    self.notifier.send("MySQL 恢复演练", summary)
            except Exception as e:
                logger.error("通知发送失败（不影响演练结果）: %s", e, exc_info=True)

    def _task_to_dict(self, task: RecoveryTask, inst: Instance, version: str) -> dict:
        return {
            "instance": inst.name,
            "host": inst.host,
            "version": version,
            "status": task.status.value if hasattr(task.status, "value") else str(task.status),
            "attempt": task.attempt,
            "duration_sec": task.duration_sec or 0,
            "verify_db": task.verify_db,
            "verify_table": task.verify_table,
            "verify_count": task.verify_count,
            "error": (task.error_msg or "")[:200],
        }

    def _dry_run(self, target_host: str, instances: list[Instance]) -> DrillResult:
        sorted_instances = sorted(instances, key=lambda i: i.mysql_version)
        logger.info("[dry-run] 目标机 %s，%d 个实例，执行顺序：", target_host, len(instances))
        for i, inst in enumerate(sorted_instances, 1):
            logger.info("[dry-run] %d. %s (%s) 备份源=%s",
                        i, inst.name, inst.mysql_version, inst.backup_source_path)
        return DrillResult(
            target_host=target_host,
            total=len(instances),
            task_results=[
                {"instance": i.name, "version": i.mysql_version, "status": "DRY_RUN"}
                for i in sorted_instances
            ],
        )
