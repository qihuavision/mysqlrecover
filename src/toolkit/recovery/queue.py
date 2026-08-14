"""任务队列与重试状态机（FP-05, Sprint 2）。

- 按版本升序、IP 升序排队（同版本连续，减少重装）
- 失败重试最多 1 次（可配）
- 状态持久化到元数据库，崩溃可续跑
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

from toolkit.core.db import get_session_ctx
from toolkit.core.logger import get_logger
from toolkit.core.models import Instance, RecoveryTask, TaskStatus
from toolkit.installer.version_manager import VersionManager

logger = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TaskQueue:
    """恢复任务队列（内存队列 + 元数据库持久化）。

    排队规则：版本升序、同版本按 IP 升序（同版本连续，减少容器切换）。
    重试规则：失败进重试队列，主队列空后处理重试，最多重试 max_retry 次。
    """

    def __init__(self, max_retry: int = 1):
        self.max_retry = max_retry
        self._todo: deque[RecoveryTask] = deque()
        self._retry: deque[RecoveryTask] = deque()
        self._all: list[RecoveryTask] = []

    # ---------- 构建队列 ----------

    def build(self, instances: list[Instance], backups_by_instance: dict[int, int]) -> list[RecoveryTask]:
        """根据实例列表 + 备份 ID 构建任务队列。

        Args:
            instances: 待演练的实例（ORM Instance 对象）
            backups_by_instance: {instance_id: backup_id}

        Returns:
            构建的任务列表（已按版本升序、IP 升序排队）
        """
        # 排序：版本升序 → IP 升序 → 名称（同版本连续，减少容器切换）
        sorted_instances = sorted(instances, key=self.sort_key)

        self._todo.clear()
        self._retry.clear()
        self._all = []

        for inst in sorted_instances:
            backup_id = backups_by_instance.get(inst.id)
            if backup_id is None:
                logger.warning("实例 %s 无可用备份，跳过", inst.name)
                continue
            task = RecoveryTask(
                run_id=0,  # 由 orchestrator 填充
                instance_id=inst.id,
                backup_id=backup_id,
                container_name="",  # 由 task_runner 执行时确定
                status=TaskStatus.PENDING,
            )
            self._todo.append(task)
            self._all.append(task)

        logger.info(
            "队列构建完成: %d 个任务（重试上限 %d）",
            len(self._all), self.max_retry,
        )
        return self._all

    # ---------- 取任务 ----------

    def next(self) -> RecoveryTask | None:
        """取下一个待执行任务。优先主队列，主队列空后取重试队列。"""
        if self._todo:
            task = self._todo.popleft()
            task.status = TaskStatus.RUNNING
            task.started_at = _now_iso()
            return task
        if self._retry:
            task = self._retry.popleft()
            # 取出重试任务时 attempt+1（表示这是第 N 次尝试）
            task.attempt = (task.attempt or 1) + 1
            task.status = TaskStatus.RETRYING
            logger.info("重试任务: instance=%s attempt=%d", task.instance_id, task.attempt)
            return task
        return None

    @property
    def pending_count(self) -> int:
        return len(self._todo) + len(self._retry)

    @property
    def all_tasks(self) -> list[RecoveryTask]:
        return self._all

    # ---------- 状态标记 ----------

    def mark_success(self, task: RecoveryTask) -> None:
        task.status = TaskStatus.SUCCESS
        task.finished_at = _now_iso()

    def mark_failed(self, task: RecoveryTask, error: str) -> bool:
        """标记失败。返回 True 表示已进入重试队列，False 表示最终失败。"""
        task.error_msg = error
        attempt = task.attempt if task.attempt is not None else 1
        if attempt <= self.max_retry:
            task.status = TaskStatus.RETRYING
            task.attempt = attempt
            self._retry.append(task)
            logger.info("任务失败进入重试队列: instance=%s, attempt=%d", task.instance_id, task.attempt)
            return True
        task.status = TaskStatus.FAILED_FINAL
        task.finished_at = _now_iso()
        logger.warning("任务最终失败: instance=%s, error=%s", task.instance_id, error[:100])
        return False

    # ---------- 断点续跑 ----------

    def load_unfinished(self, run_id: int) -> list[RecoveryTask]:
        """从元数据库加载未完成的任务（断点续跑）。

        场景：工具崩溃后重启，从 recovery_tasks 表加载
        status 为 PENDING/RUNNING/RETRYING 的任务继续执行。
        """
        session = get_session_ctx()
        try:
            tasks = (
                session.query(RecoveryTask)
                .filter(
                    RecoveryTask.run_id == run_id,
                    RecoveryTask.status.in_[
                        TaskStatus.PENDING.value,
                        TaskStatus.RUNNING.value,
                        TaskStatus.RETRYING.value,
                    ]),
                )
                .all()
            )
            # 重置 RUNNING 状态（崩溃时正在跑的任务）为 PENDING
            for t in tasks:
                if t.status == TaskStatus.RUNNING.value:
                    t.status = TaskStatus.PENDING.value
            session.commit()

            self._todo.clear()
            self._retry.clear()
            self._all = list(tasks)
            for t in tasks:
                if t.status == TaskStatus.RETRYING.value:
                    self._retry.append(t)
                else:
                    self._todo.append(t)

            logger.info("断点续跑: 从 run_id=%d 加载 %d 个未完成任务", run_id, len(tasks))
            return list(tasks)
        finally:
            session.close()

    def persist_task(self, task: RecoveryTask) -> None:
        """任务状态变更后持久化到数据库（断点续跑的保障）。"""
        session = get_session_ctx()
        try:
            db_task = session.query(RecoveryTask).filter_by(id=task.id).first()
            if db_task:
                db_task.status = task.status.value if hasattr(task.status, "value") else str(task.status)
                db_task.attempt = task.attempt
                db_task.error_msg = task.error_msg
                db_task.finished_at = task.finished_at
                db_task.duration_sec = task.duration_sec
                db_task.verify_db = task.verify_db
                db_task.verify_table = task.verify_table
                db_task.verify_count = task.verify_count
                db_task.updated_at = _now_iso()
                session.commit()
        finally:
            session.close()

    # ---------- 排序 ----------

    @staticmethod
    def sort_key(instance) -> tuple:
        """排序键：版本元组升序 + IP 升序。同版本连续，减少重装。"""
        version_tuple = tuple(int(x) for x in instance.mysql_version.split("."))
        return (version_tuple, instance.host, instance.name)
