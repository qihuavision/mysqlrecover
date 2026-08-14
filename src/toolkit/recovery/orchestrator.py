"""恢复编排主流程（FP-03 核心, Sprint 2）。

职责：收集备份 → 建队列 → 逐个执行 → 失败重试 → 聚合结果。
不直接做 xtrabackup/启停容器，全部委托给 task_runner + installer + locator。

编排逻辑：
1. 创建 drill_run 批次记录
2. 定位各实例最新备份（无备份的跳过）
3. 构建任务队列（版本升序、IP 升序）
4. 逐个执行任务，失败进重试队列（最多重试 max_retry 次）
5. 每个任务状态实时持久化（断点续跑保障）
6. 聚合 DrillResult，更新批次统计
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from toolkit.core.db import get_session_ctx
from toolkit.core.logger import get_logger
from toolkit.core.models import Backup, DrillRun, Instance, RecoveryTask, TaskStatus
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
    task_results: list[dict] = field(default_factory=list)


class Orchestrator:
    """恢复演练编排引擎（Sprint 2 实现，Sprint 3 加报告+通知）。"""

    def __init__(
        self,
        task_runner: RecoveryTaskRunner,
        queue: TaskQueue | None = None,
        max_retry: int = 1,
        reporter=None,  # MarkdownReporter，None 则不出报告
        notifier=None,  # WeComNotifier，None 则不发通知
        archive_root: str = "",
    ):
        self.task_runner = task_runner
        self.queue = queue or TaskQueue(max_retry=max_retry)
        self.max_retry = max_retry
        self.reporter = reporter
        self.notifier = notifier
        self.archive_root = archive_root

    # ---------- 主入口 ----------

    def run(
        self,
        target_host: str,
        instances: list[Instance],
        dry_run: bool = True,
        resume_run_id: int | None = None,
    ) -> DrillResult:
        """执行一次完整演练（多实例编排）。

        Args:
            target_host: 恢复目标机
            instances: 待演练实例（ORM 对象，需已入库）
            dry_run: 只打印不执行
            resume_run_id: 断点续跑的批次 ID（None = 新批次）

        Returns:
            DrillResult
        """
        start = datetime.now(timezone.utc)

        if dry_run:
            return self._dry_run(target_host, instances)

        # 1. 创建批次（或续跑）
        if resume_run_id:
            run_id = resume_run_id
            logger.info("断点续跑批次 #%d", run_id)
            self.queue.load_unfinished(run_id)
        else:
            run_id = self._create_run(target_host, len(instances))

        result = DrillResult(run_id=run_id, target_host=target_host, total=len(instances))

        # 2. 新批次：定位备份 + 建队列
        if not resume_run_id:
            backups_map, skipped = self._locate_backups(instances)
            result.skipped = skipped
            self.queue.build(instances, backups_map)
            # 把 run_id 填进任务并入库
            self._register_tasks(run_id)

        # 3. 逐个执行
        while True:
            task = self.queue.next()
            if task is None:
                break

            inst = self._get_instance(task.instance_id)
            if inst is None:
                self.queue.mark_failed(task, f"实例 {task.instance_id} 不存在")
                continue

            backup_path = self._get_backup_path(task.backup_id)
            logger.info(">>> [%s] 开始演练 (v%s, 第%d次)", inst.name, inst.mysql_version, task.attempt)

            task_result = self.task_runner.execute(
                mysql_version=inst.mysql_version,
                backup_remote_path=backup_path,
                instance_name=inst.name,
                backup_source_host=inst.backup_source_host,
                dry_run=False,
            )

            # 填充任务结果字段
            task.duration_sec = task_result.duration_sec
            task.verify_db = task_result.verify_db
            task.verify_table = task_result.verify_table
            task.verify_count = task_result.verify_count

            if task_result.success:
                self.queue.mark_success(task)
                result.success += 1
                logger.info("<<< [%s] ✅ 成功 (%ds)", inst.name, task_result.duration_sec)
            else:
                # RUNNING 状态的任务失败，attempt 还是原值 → mark_failed 决定重试
                retried = self.queue.mark_failed(task, task_result.error_msg)
                if retried:
                    result.retried += 1
                else:
                    result.failed += 1
                logger.warning("<<< [%s] ❌ 失败: %s", inst.name, task_result.error_msg[:100])

            # 实时持久化（断点续跑保障）
            if task.id:
                self.queue.persist_task(task)

            result.task_results.append(self._task_to_dict(task, inst))

        # 4. 收尾：更新批次 + 报告 + 通知
        result.duration_sec = int((datetime.now(timezone.utc) - start).total_seconds())
        self._finish_run(run_id, result)
        self._report_and_notify(result)
        return result

    # ---------- 内部方法 ----------

    def _create_run(self, target_host: str, total: int) -> int:
        """创建演练批次记录。"""
        session = get_session_ctx()
        try:
            run = DrillRun(target_host=target_host, total_count=total)
            session.add(run)
            session.commit()
            logger.info("创建演练批次 #%d（%d 个实例）", run.id, total)
            return run.id
        finally:
            session.close()

    def _locate_backups(self, instances: list[Instance]) -> tuple[dict[int, int], int]:
        """定位各实例最新备份。

        优先用已登记的备份（backup scan 产出）；无登记备份时降级用
        instance.backup_source_path 自动登记（联调/简单场景）。

        Returns:
            (backups_map, skipped_count)
            backups_map: {instance_id: backup_id}
        """
        session = get_session_ctx()
        backups_map: dict[int, int] = {}
        skipped = 0
        try:
            for inst in instances:
                backup = (
                    session.query(Backup)
                    .filter_by(instance_id=inst.id, status="available")
                    .order_by(Backup.finished_at.desc())
                    .first()
                )
                if backup:
                    backups_map[inst.id] = backup.id
                    continue
                # 降级：无登记备份，用配置的 backup_source_path 自动登记
                if inst.backup_source_path:
                    backup = Backup(
                        instance_id=inst.id,
                        backup_path=inst.backup_source_path,
                        status="available",
                    )
                    session.add(backup)
                    session.flush()  # 拿到 id
                    backups_map[inst.id] = backup.id
                    logger.info("实例 %s 无登记备份，已用配置路径自动登记: %s",
                                inst.name, inst.backup_source_path)
                else:
                    skipped += 1
                    logger.warning("实例 %s 无可用备份，跳过", inst.name)
            session.commit()
            return backups_map, skipped
        finally:
            session.close()

    def _register_tasks(self, run_id: int) -> None:
        """把队列中的任务写入数据库（拿到 task.id 供后续持久化）。"""
        session = get_session_ctx()
        try:
            for task in self.queue.all_tasks:
                task.run_id = run_id
                task.status = TaskStatus.PENDING
                session.add(task)
            session.commit()
            logger.info("已登记 %d 个任务到批次 #%d", len(self.queue.all_tasks), run_id)
        finally:
            session.close()

    def _get_instance(self, instance_id: int) -> Instance | None:
        session = get_session_ctx()
        try:
            return session.query(Instance).filter_by(id=instance_id).first()
        finally:
            session.close()

    def _get_backup_path(self, backup_id: int) -> str:
        session = get_session_ctx()
        try:
            backup = session.query(Backup).filter_by(id=backup_id).first()
            return backup.backup_path if backup else ""
        finally:
            session.close()

    def _finish_run(self, run_id: int, result: DrillResult) -> None:
        """批次收尾：更新统计。"""
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

    def _task_to_dict(self, task: RecoveryTask, inst: Instance) -> dict:
        """任务结果转 dict（供报告）。"""
        return {
            "instance": inst.name,
            "host": inst.host,
            "version": inst.mysql_version,
            "status": task.status.value if hasattr(task.status, "value") else str(task.status),
            "attempt": task.attempt,
            "duration_sec": task.duration_sec or 0,
            "verify_db": task.verify_db,
            "verify_table": task.verify_table,
            "verify_count": task.verify_count,
            "error": (task.error_msg or "")[:200],
        }

    def _report_and_notify(self, result: DrillResult) -> None:
        """出报告 + 发通知（Sprint 3）。失败不阻塞主流程。"""
        report_path = ""
        # 1. Markdown 报告
        if self.reporter:
            try:
                report_path = str(self.reporter.render(result, archive_root=self.archive_root))
            except Exception as e:
                logger.error("报告生成失败（不影响演练结果）: %s", e, exc_info=True)

        # 2. 企业微信通知
        if self.notifier:
            try:
                if self.notifier.should_notify(result.failed):
                    summary = self.reporter.summarize_markdown(result) if self.reporter else ""
                    if report_path:
                        summary += f"\n\n📄 报告：`{report_path}`"
                    self.notifier.send("MySQL 恢复演练", summary)
            except Exception as e:
                logger.error("通知发送失败（不影响演练结果）: %s", e, exc_info=True)

    def _dry_run(self, target_host: str, instances: list[Instance]) -> DrillResult:
        """dry-run：只打印执行计划。"""
        sorted_instances = sorted(instances, key=self.queue.sort_key)
        logger.info("[dry-run] 目标机 %s，%d 个实例，执行顺序：", target_host, len(instances))
        for i, inst in enumerate(sorted_instances, 1):
            logger.info("[dry-run] %d. %s (%s) 备份源=%s", i, inst.name, inst.mysql_version, inst.backup_source_path)
        return DrillResult(
            target_host=target_host,
            total=len(instances),
            task_results=[
                {"instance": i.name, "version": i.mysql_version, "status": "DRY_RUN"}
                for i in sorted_instances
            ],
        )
