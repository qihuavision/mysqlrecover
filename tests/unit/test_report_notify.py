"""Sprint 3 单元测试：MarkdownReporter + WeComNotifier + BackupRunner。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from toolkit.core.db import init_db, reset_db
from toolkit.core.exceptions import RecoveryError
from toolkit.core.executor import ExecResult, FakeExecutor
from toolkit.core.models import Instance
from toolkit.recovery.orchestrator import DrillResult
from toolkit.reporters.markdown import MarkdownReporter
from toolkit.notifiers.wecom import WeComNotifier
from toolkit.backup.runner import BackupRunner


@pytest.fixture
def db():
    init_db("sqlite:///:memory:")
    yield
    reset_db()


def _make_result() -> DrillResult:
    return DrillResult(
        run_id=42, target_host="192.168.1.15",
        total=3, success=2, failed=1, retried=1, skipped=0, duration_sec=100,
        task_results=[
            {"instance": "db1", "version": "8.0.35", "status": "SUCCESS", "attempt": 1,
             "duration_sec": 20, "verify_db": "orders", "verify_table": "order_info",
             "verify_count": 100, "error": "", "log_dir": "/a/b"},
            {"instance": "db2", "version": "8.0.35", "status": "FAILED_FINAL", "attempt": 2,
             "duration_sec": 30, "verify_db": "", "verify_table": "",
             "verify_count": -1, "error": "容器启动失败", "log_dir": "/a/c"},
            {"instance": "db3", "version": "5.7.44", "status": "SUCCESS", "attempt": 1,
             "duration_sec": 15, "verify_db": "users", "verify_table": "user",
             "verify_count": 50, "error": "", "log_dir": "/a/d"},
        ],
    )


class TestMarkdownReporter:
    def test_render_creates_file(self, tmp_path):
        reporter = MarkdownReporter(output_dir=str(tmp_path / "reports"))
        result = _make_result()
        path = reporter.render(result, archive_root="/data/archive")
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        # 汇总数据正确
        assert "42" in content          # run_id
        assert "3" in content           # total
        assert "2" in content           # success
        assert "100" in content         # duration_sec 不为空（防回归）
        # 明细包含实例
        assert "db1" in content
        assert "db2" in content
        assert "db3" in content
        # 失败实例的错误
        assert "容器启动失败" in content
        # 验证信息
        assert "orders.order_info" in content or "order_info" in content

    def test_render_all_success(self, tmp_path):
        reporter = MarkdownReporter(output_dir=str(tmp_path))
        result = DrillResult(run_id=1, total=2, success=2, failed=0,
                            task_results=[
                                {"instance": "a", "version": "8.0", "status": "SUCCESS",
                                 "attempt": 1, "duration_sec": 5, "verify_db": "x",
                                 "verify_table": "y", "verify_count": 1, "error": "", "log_dir": "/l"},
                            ] * 2)
        path = reporter.render(result)
        content = path.read_text(encoding="utf-8")
        assert "全部恢复成功" in content

    def test_summarize_markdown(self):
        result = _make_result()
        summary = MarkdownReporter.summarize_markdown(result)
        assert "2/3" in summary          # 成功/尝试
        assert "失败" in summary
        assert "db2" in summary          # 失败实例
        assert "容器启动失败" in summary

    def test_summarize_no_failure(self):
        result = DrillResult(run_id=1, total=2, success=2, failed=0, duration_sec=10,
                             task_results=[])
        summary = MarkdownReporter.summarize_markdown(result)
        assert "✅" in summary
        assert "完成" in summary


class TestWeComNotifier:
    def test_no_webhook_returns_false(self, monkeypatch):
        monkeypatch.delenv("DRILL_WECOM_WEBHOOK", raising=False)
        n = WeComNotifier()
        assert n.send("title", "content") is False

    def test_send_success(self, monkeypatch):
        monkeypatch.setenv("DRILL_WECOM_WEBHOOK", "https://example.test/hook")
        n = WeComNotifier()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"errcode": 0}
        with patch("toolkit.notifiers.wecom.requests.post", return_value=mock_resp) as mp:
            assert n.send("标题", "内容") is True
            # 校验 payload
            args, kwargs = mp.call_args
            assert kwargs["json"]["msgtype"] == "markdown"
            assert "标题" in kwargs["json"]["markdown"]["content"]

    def test_send_retry_then_success(self, monkeypatch):
        monkeypatch.setenv("DRILL_WECOM_WEBHOOK", "https://example.test/hook")
        n = WeComNotifier(max_retry=3)
        err_resp = MagicMock()
        err_resp.json.return_value = {"errcode": 500}
        ok_resp = MagicMock()
        ok_resp.json.return_value = {"errcode": 0}
        with patch("toolkit.notifiers.wecom.requests.post",
                   side_effect=[err_resp, err_resp, ok_resp]):
            with patch("toolkit.notifiers.wecom.time.sleep"):  # 跳过退避等待
                assert n.send("t", "c") is True

    def test_should_notify_on_failure_strategy(self):
        n_always = WeComNotifier(notify_on="always")
        n_fail = WeComNotifier(notify_on="on_failure")
        assert n_always.should_notify(0) is True
        assert n_always.should_notify(5) is True
        assert n_fail.should_notify(0) is False
        assert n_fail.should_notify(5) is True


class TestBackupRunner:
    def _make_instance(self, db) -> Instance:
        from toolkit.core.db import get_session_ctx
        session = get_session_ctx()
        try:
            inst = Instance(name="bk1", host="10.0.0.1", mysql_version="8.0.35",
                            backup_source_host="10.0.0.9", backup_source_path="/b")
            session.add(inst)
            session.commit()
            session.refresh(inst)
            return inst
        finally:
            session.close()

    def test_run_backup_success(self, db, monkeypatch):
        monkeypatch.setenv("DRILL_MYSQL_PWD", "pwd123")
        inst = self._make_instance(db)
        fake = FakeExecutor()
        # 磁盘检查
        fake.run_results["df -BG --output=avail /data/backups 2>/dev/null | tail -1 | tr -dc '0-9'"] = \
            ExecResult(0, "100\n", "")
        # xtrabackup 备份成功（defaults-extra-file 在第一位）
        fake.run_results[
            "/usr/bin/xtrabackup --defaults-extra-file=/tmp/.xb_creds_bk1_TESTTIME --backup --host=127.0.0.1 --port=3306 --target-dir=/data/backups/bk1/TESTTIME --no-lock 2>&1"
        ] = ExecResult(0, "... completed OK!\n", "")
        # 完整性检查
        fake.run_results[
            "test -f /data/backups/bk1/TESTTIME/xtrabackup_checkpoints && echo yes || echo no"
        ] = ExecResult(0, "yes\n", "")
        # 大小
        fake.run_results["du -sb /data/backups/bk1/TESTTIME 2>/dev/null | cut -f1"] = \
            ExecResult(0, "1024000\n", "")

        runner = BackupRunner(executor=fake)
        # 固定时间戳便于 mock 命令匹配
        with patch("toolkit.backup.runner.datetime") as mock_dt:
            mock_dt.now.return_value.strftime.return_value = "TESTTIME"
            backup_id = runner.run_backup(inst, "/data/backups")

        assert backup_id > 0
        # 验证登记
        from toolkit.core.db import get_session_ctx
        from toolkit.core.models import Backup
        session = get_session_ctx()
        try:
            record = session.query(Backup).filter_by(id=backup_id).first()
            assert record is not None
            assert record.status == "available"
            assert record.size_bytes == 1024000
        finally:
            session.close()
        # 凭据文件应被清理
        cmds = [c[1][0] for c in fake.calls if c[0] == "run"]
        assert any("rm -f /tmp/.xb_creds_bk1_TESTTIME" in c for c in cmds)

    def test_run_backup_disk_full(self, db):
        inst = self._make_instance(db)
        fake = FakeExecutor()
        fake.run_results["df -BG --output=avail /data/backups 2>/dev/null | tail -1 | tr -dc '0-9'"] = \
            ExecResult(0, "5\n", "")  # 只剩 5G

        runner = BackupRunner(executor=fake, min_free_gb=10)
        from toolkit.core.exceptions import DiskFullError
        with pytest.raises(DiskFullError):
            runner.run_backup(inst, "/data/backups")

    def test_run_backup_xb_failure(self, db, monkeypatch):
        monkeypatch.setenv("DRILL_MYSQL_PWD", "pwd")
        inst = self._make_instance(db)
        fake = FakeExecutor()
        fake.run_results["df -BG --output=avail /data/backups 2>/dev/null | tail -1 | tr -dc '0-9'"] = \
            ExecResult(0, "500\n", "")
        # xtrabackup 失败（无 completed OK）
        runner = BackupRunner(executor=fake)
        with patch("toolkit.backup.runner.datetime") as mock_dt:
            mock_dt.now.return_value.strftime.return_value = "T1"
            with pytest.raises(RecoveryError, match="备份"):
                runner.run_backup(inst, "/data/backups")
