"""BackupLocator + Xtrabackup 单元测试（用 FakeExecutor）。"""
from __future__ import annotations

import pytest

from toolkit.backup.locator import BackupLocator
from toolkit.backup.xtrabackup import Xtrabackup
from toolkit.core.exceptions import RecoveryError
from toolkit.core.executor import ExecResult, FakeExecutor


class TestBackupLocator:
    def test_find_latest_success(self):
        fake = FakeExecutor()
        # ls 返回最新备份目录
        fake.run_results[
            "ls -1d /backups/db1/*/ 2>/dev/null | sort -r | head -1"
        ] = ExecResult(0, "/backups/db1/20260813/\n", "")
        # is_complete 检查通过
        fake.run_results[
            "test -f /backups/db1/20260813/xtrabackup_checkpoints && echo yes || echo no"
        ] = ExecResult(0, "yes\n", "")

        locator = BackupLocator(fake)
        result = locator.find_latest("/backups/db1")
        assert result == "/backups/db1/20260813"

    def test_find_latest_no_backup(self):
        fake = FakeExecutor()
        fake.run_results[
            "ls -1d /backups/db1/*/ 2>/dev/null | sort -r | head -1"
        ] = ExecResult(0, "", "")  # 空
        locator = BackupLocator(fake)
        assert locator.find_latest("/backups/db1") is None

    def test_find_latest_incomplete_skipped(self):
        fake = FakeExecutor()
        fake.run_results[
            "ls -1d /backups/db1/*/ 2>/dev/null | sort -r | head -1"
        ] = ExecResult(0, "/backups/db1/20260813/\n", "")
        # is_complete 返回 no（缺 checkpoints）
        fake.run_results[
            "test -f /backups/db1/20260813/xtrabackup_checkpoints && echo yes || echo no"
        ] = ExecResult(0, "no\n", "")
        locator = BackupLocator(fake)
        assert locator.find_latest("/backups/db1") is None

    def test_get_backup_info(self):
        fake = FakeExecutor()
        fake.run_results[
            "cat /backups/db1/full/xtrabackup_checkpoints 2>/dev/null"
        ] = ExecResult(0, "backup_type = full-backuped\nfrom_lsn = 0\nto_lsn = 12345\n", "")
        locator = BackupLocator(fake)
        info = locator.get_backup_info("/backups/db1/full")
        assert info["backup_type"] == "full-backuped"
        assert info["to_lsn"] == "12345"


class TestXtrabackup:
    def test_prepare_success(self):
        fake = FakeExecutor()
        fake.run_results[
            "/usr/bin/xtrabackup --prepare --target-dir=/tmp/bak"
        ] = ExecResult(0, "... completed OK!\n...", "")
        xb = Xtrabackup(fake)
        log = xb.prepare("/tmp/bak")
        assert "completed OK!" in log

    def test_prepare_failure_raises(self):
        fake = FakeExecutor()
        fake.run_results[
            "/usr/bin/xtrabackup --prepare --target-dir=/tmp/bak"
        ] = ExecResult(1, "error", "failed")
        xb = Xtrabackup(fake)
        with pytest.raises(RecoveryError):
            xb.prepare("/tmp/bak")

    def test_prepare_no_completed_ok_raises(self):
        fake = FakeExecutor()
        fake.run_results[
            "/usr/bin/xtrabackup --prepare --target-dir=/tmp/bak"
        ] = ExecResult(0, "some output without ok", "")
        xb = Xtrabackup(fake)
        with pytest.raises(RecoveryError, match="completed OK"):
            xb.prepare("/tmp/bak")

    def test_copy_back_success(self):
        fake = FakeExecutor()
        cmd = "/usr/bin/xtrabackup --copy-back --target-dir=/tmp/bak --datadir=/data/drill/8.0.35/datadir"
        fake.run_results[cmd] = ExecResult(0, "completed OK!\n", "")
        xb = Xtrabackup(fake)
        xb.copy_back("/tmp/bak", "/data/drill/8.0.35/datadir")

    def test_copy_back_failure_raises(self):
        fake = FakeExecutor()
        cmd = "/usr/bin/xtrabackup --copy-back --target-dir=/tmp/bak --datadir=/data/drill/8.0.35/datadir"
        fake.run_results[cmd] = ExecResult(1, "", "datadir not empty")
        xb = Xtrabackup(fake)
        with pytest.raises(RecoveryError):
            xb.copy_back("/tmp/bak", "/data/drill/8.0.35/datadir")

    def test_check_log_ok(self):
        assert Xtrabackup.check_log_ok("blah completed OK! blah") is True
        assert Xtrabackup.check_log_ok("failed") is False
