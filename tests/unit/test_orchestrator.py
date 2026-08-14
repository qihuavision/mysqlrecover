"""TaskQueue + Orchestrator 单元测试（Sprint 2）。"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from toolkit.core.db import get_session_ctx, init_db, reset_db
from toolkit.core.models import Backup, Instance, RecoveryTask, TaskStatus
from toolkit.recovery.orchestrator import DrillResult, Orchestrator
from toolkit.recovery.queue import TaskQueue
from toolkit.recovery.task import TaskResult


@pytest.fixture
def db():
    init_db("sqlite:///:memory:")
    yield
    reset_db()


def _make_instance(name="db1", version="8.0.35", host="10.0.0.1") -> Instance:
    return Instance(
        name=name, host=host, port=3306, mysql_version=version,
        backup_source_host="10.0.0.9", backup_source_path=f"/backups/{name}",
    )


def _make_task(instance_id=1, backup_id=1) -> RecoveryTask:
    return RecoveryTask(
        run_id=1, instance_id=instance_id, backup_id=backup_id,
        container_name="drill-mysql-8035",
    )


class TestTaskQueue:
    def test_build_orders_by_version_then_ip(self, db):
        """队列应按版本升序、同版本按 IP 排序。"""
        session = get_session_ctx()
        try:
            # 乱序插入
            insts = [
                _make_instance("db-b", "8.0.35", "10.0.0.2"),
                _make_instance("db-a", "5.7.44", "10.0.0.5"),
                _make_instance("db-c", "8.0.35", "10.0.0.1"),
                _make_instance("db-d", "5.7.44", "10.0.0.3"),
            ]
            session.add_all(insts)
            session.commit()
            ids = [i.id for i in insts]
        finally:
            session.close()

        q = TaskQueue(max_retry=1)
        q.build(insts, {i.id: 1 for i in insts})

        order = []
        while (t := q.next()) is not None:
            order.append(t.instance_id)
        # 期望：5.7.44 的两个（按 IP 3,5）→ 8.0.35 的两个（按 IP 1,2）
        # insts 顺序: db-b(8.0.35,10.0.0.2)=ids[0], db-a(5.7.44,10.0.0.5)=ids[1],
        #             db-c(8.0.35,10.0.0.1)=ids[2], db-d(5.7.44,10.0.0.3)=ids[3]
        assert order == [ids[3], ids[1], ids[2], ids[0]]

    def test_next_returns_none_when_empty(self):
        q = TaskQueue()
        assert q.next() is None

    def test_mark_success(self):
        q = TaskQueue()
        t = _make_task()
        q.mark_success(t)
        assert t.status == TaskStatus.SUCCESS
        assert t.finished_at

    def test_mark_failed_first_time_enters_retry(self):
        q = TaskQueue(max_retry=1)
        t = _make_task()
        t.attempt = 1
        t.status = TaskStatus.RUNNING
        retried = q.mark_failed(t, "some error")
        assert retried is True
        assert t.status == TaskStatus.RETRYING
        assert t.error_msg == "some error"

    def test_mark_failed_second_time_final(self):
        q = TaskQueue(max_retry=1)
        t = _make_task()
        t.attempt = 2  # 已经是重试
        t.status = TaskStatus.RETRYING
        retried = q.mark_failed(t, "still failing")
        assert retried is False
        assert t.status == TaskStatus.FAILED_FINAL

    def test_retry_queue_processed_after_main(self):
        """重试任务在主队列之后处理。"""
        q = TaskQueue(max_retry=1)
        t1 = _make_task(instance_id=1)
        t2 = _make_task(instance_id=2)
        q._todo.append(t1)
        q._todo.append(t2)

        # 取第一个并失败 → 进重试
        first = q.next()
        assert first is t1
        first.status = TaskStatus.RUNNING
        q.mark_failed(first, "err")

        # 主队列还有 t2
        second = q.next()
        assert second is t2
        # 主队列空后取重试的 t1
        third = q.next()
        assert third is t1
        assert third.attempt == 2  # 取重试时 attempt+1

    def test_build_skips_instances_without_backup(self, db):
        session = get_session_ctx()
        try:
            insts = [_make_instance("has-backup"), _make_instance("no-backup")]
            session.add_all(insts)
            session.commit()
        finally:
            session.close()

        q = TaskQueue()
        tasks = q.build(insts, {insts[0].id: 1})  # 只有第一个有备份
        assert len(tasks) == 1


class TestOrchestratorDryRun:
    def test_dry_run_returns_plan(self, db):
        session = get_session_ctx()
        try:
            insts = [_make_instance("b", "8.0.35"), _make_instance("a", "5.7.44")]
            session.add_all(insts)
            session.commit()
        finally:
            session.close()

        runner = MagicMock()
        orch = Orchestrator(task_runner=runner)
        result = orch.run("10.0.0.100", insts, dry_run=True)
        assert result.total == 2
        runner.execute.assert_not_called()


class TestOrchestratorRun:
    def _setup(self, db):
        """准备实例 + 备份。"""
        session = get_session_ctx()
        try:
            inst = _make_instance("db1")
            session.add(inst)
            session.commit()
            backup = Backup(instance_id=inst.id, backup_path="/backups/db1/latest", status="available")
            session.add(backup)
            session.commit()
            return inst, backup
        finally:
            session.close()

    def test_success_flow(self, db):
        inst, backup = self._setup(db)
        runner = MagicMock()
        runner.execute.return_value = TaskResult(
            success=True, duration_sec=20, container_name="drill-mysql-8035",
        )
        orch = Orchestrator(task_runner=runner, max_retry=1)
        result = orch.run("10.0.0.100", [inst], dry_run=False)

        assert result.total == 1
        assert result.success == 1
        assert result.failed == 0
        runner.execute.assert_called_once()

    def test_failure_then_retry_success(self, db):
        """失败 → 重试 → 成功。"""
        inst, backup = self._setup(db)
        runner = MagicMock()
        # 第一次失败，第二次（重试）成功
        runner.execute.side_effect = [
            TaskResult(success=False, error_msg="first fail"),
            TaskResult(success=True, duration_sec=25),
        ]
        orch = Orchestrator(task_runner=runner, max_retry=1)
        result = orch.run("10.0.0.100", [inst], dry_run=False)

        assert result.success == 1
        assert result.retried == 1  # 重试了一次
        assert result.failed == 0
        assert runner.execute.call_count == 2

    def test_failure_retry_failure_final(self, db):
        """失败 → 重试 → 再失败 → FAILED_FINAL。"""
        inst, backup = self._setup(db)
        runner = MagicMock()
        runner.execute.return_value = TaskResult(success=False, error_msg="always fail")
        orch = Orchestrator(task_runner=runner, max_retry=1)
        result = orch.run("10.0.0.100", [inst], dry_run=False)

        assert result.success == 0
        assert result.failed == 1
        assert result.retried == 1
        assert runner.execute.call_count == 2  # 原始 + 重试
