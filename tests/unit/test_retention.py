"""retention.py 单元测试（Sprint 4）。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from toolkit.core.db import get_session_ctx, init_db, reset_db
from toolkit.core.executor import ExecResult, FakeExecutor
from toolkit.core.models import DrillRun, Instance, RecoveryLog, RecoveryTask
from toolkit.core.retention import (
    cleanup_local_reports,
    cleanup_old_tasks,
    cleanup_remote_backups,
)


@pytest.fixture
def db():
    init_db("sqlite:///:memory:")
    yield
    reset_db()


def _old_ts(days: int) -> str:
    from datetime import timezone
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestCleanupOldTasks:
    def _setup_tasks(self, db):
        """造数据：1 新任务 + 2 旧任务（不同终态）+ 关联日志。"""
        session = get_session_ctx()
        try:
            inst = Instance(name="i1", host="10.0.0.1", mysql_version="8.0.35",
                            backup_source_host="h", backup_source_path="/p")
            run = DrillRun(target_host="t")
            session.add_all([inst, run])
            session.commit()

            # 新任务（30 天前，SUCCESS）
            t_new = RecoveryTask(run_id=run.id, instance_id=inst.id, backup_id=1,
                                 container_name="c", status="SUCCESS",
                                 created_at=_old_ts(30), updated_at=_old_ts(30))
            # 旧任务（200 天前，SUCCESS —— 应被清理）
            t_old1 = RecoveryTask(run_id=run.id, instance_id=inst.id, backup_id=1,
                                  container_name="c", status="SUCCESS",
                                  created_at=_old_ts(200), updated_at=_old_ts(200))
            # 旧任务（200 天前，FAILED_FINAL —— 应被清理）
            t_old2 = RecoveryTask(run_id=run.id, instance_id=inst.id, backup_id=1,
                                  container_name="c", status="FAILED_FINAL",
                                  created_at=_old_ts(200), updated_at=_old_ts(200))
            # 旧任务（200 天前，PENDING —— 未终态，不清理）
            t_old_pending = RecoveryTask(run_id=run.id, instance_id=inst.id, backup_id=1,
                                         container_name="c", status="PENDING",
                                         created_at=_old_ts(200), updated_at=_old_ts(200))
            session.add_all([t_new, t_old1, t_old2, t_old_pending])
            session.commit()

            # 日志：旧的（挂在 t_old1 上）+ 新的（挂在 t_new 上）
            session.add_all([
                RecoveryLog(task_id=t_old1.id, log_dir="/old", created_at=_old_ts(200)),
                RecoveryLog(task_id=t_new.id, log_dir="/new", created_at=_old_ts(30)),
            ])
            session.commit()
        finally:
            session.close()

    def test_cleanup_removes_old_final_tasks(self, db):
        self._setup_tasks(db)
        removed = cleanup_old_tasks(keep_days=180)
        assert removed == 2  # t_old1 + t_old2

        session = get_session_ctx()
        try:
            # 新任务和未终态任务保留
            remaining = session.query(RecoveryTask).count()
            assert remaining == 2
            statuses = {t.status for t in session.query(RecoveryTask).all()}
            assert "SUCCESS" in statuses
            assert "PENDING" in statuses
            # 旧日志被清理，新日志保留
            logs = session.query(RecoveryLog).count()
            assert logs == 1
        finally:
            session.close()

    def test_cleanup_keeps_recent(self, db):
        """保留期内不清理。"""
        self._setup_tasks(db)
        removed = cleanup_old_tasks(keep_days=365)  # 1 年内都保留
        assert removed == 0


class TestCleanupLocalReports:
    def test_removes_old_reports(self, tmp_path):
        # 造新旧两个报告
        old_file = tmp_path / "drill-run1-20250101000000.md"
        old_file.write_text("old")
        new_file = tmp_path / "drill-run2-20260814000000.md"
        new_file.write_text("new")
        # 设置 mtime
        import os
        old_time = (datetime.now() - timedelta(days=100)).timestamp()
        os.utime(old_file, (old_time, old_time))

        removed = cleanup_local_reports(str(tmp_path), keep_days=90)
        assert removed == 1
        assert not old_file.exists()
        assert new_file.exists()

    def test_dir_not_exist_returns_zero(self):
        assert cleanup_local_reports("/nonexistent/path") == 0


class TestCleanupRemoteBackups:
    def test_removes_expired_dirs(self):
        fake = FakeExecutor()
        fake.run_results[
            "find /data/drill/tmp-backups -maxdepth 2 -mindepth 2 -type d -mtime +7 2>/dev/null"
        ] = ExecResult(0, "/data/drill/tmp-backups/inst1/20260101\n", "")

        removed = cleanup_remote_backups(fake, "/data/drill/tmp-backups", keep_days=7)
        assert removed == 1
        cmds = [c[1][0] for c in fake.calls if c[0] == "run"]
        assert any("rm -rf /data/drill/tmp-backups/inst1/20260101" in c for c in cmds)

    def test_no_expired_dirs(self):
        fake = FakeExecutor()
        fake.run_results[
            "find /data/drill/tmp-backups -maxdepth 2 -mindepth 2 -type d -mtime +7 2>/dev/null"
        ] = ExecResult(0, "", "")
        assert cleanup_remote_backups(fake, "/data/drill/tmp-backups", keep_days=7) == 0
