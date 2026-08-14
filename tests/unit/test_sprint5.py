"""Sprint 5 单元测试：PortAllocator + 版本自动检测 + BackupPuller + 版本矩阵。"""
from __future__ import annotations

import pytest

from toolkit.core.executor import ExecResult, FakeExecutor
from toolkit.core.port_allocator import PortAllocator
from toolkit.backup.locator import BackupLocator
from toolkit.backup.puller import BackupPuller
from toolkit.backup.xtrabackup import Xtrabackup


class TestPortAllocator:
    def test_assign_multiple_versions(self):
        """多版本按升序分配连续端口。"""
        pa = PortAllocator(base_port=13306)
        ports = pa.assign(["8.0.35", "5.7.44"])
        assert ports == {"5.7.44": 13306, "8.0.35": 13307}

    def test_assign_idempotent(self):
        """重复分配结果一致。"""
        pa = PortAllocator()
        p1 = pa.assign(["8.0.35", "5.7.44"])
        p2 = pa.assign(["8.0.35", "5.7.44"])
        assert p1 == p2

    def test_get_or_assign_new_version(self):
        """动态追加版本。"""
        pa = PortAllocator()
        pa.assign(["5.7.44"])                 # 5.7.44 → 13306
        port = pa.get_or_assign("8.0.35")     # 8.0.35 → 13307
        assert port == 13307
        assert pa.get("5.7.44") == 13306      # 旧分配不变

    def test_three_versions(self):
        pa = PortAllocator(base_port=13306)
        ports = pa.assign(["8.0.36", "5.7.44", "8.0.35"])
        assert ports == {"5.7.44": 13306, "8.0.35": 13307, "8.0.36": 13308}


class TestDetectMySQLVersion:
    def test_detect_from_xtrabackup_info(self):
        fake = FakeExecutor()
        fake.run_results["cat /backups/db1/xtrabackup_info 2>/dev/null"] = ExecResult(
            0, "tool_version = 8.0.35-31\nserver_version = 8.0.35\nstart_time = 2026-08-14\nbackup_type = full-backuped\n", ""
        )
        locator = BackupLocator(fake)
        assert locator.detect_mysql_version("/backups/db1") == "8.0.35"

    def test_detect_57(self):
        fake = FakeExecutor()
        fake.run_results["cat /b/xtrabackup_info 2>/dev/null"] = ExecResult(
            0, "server_version = 5.7.44\n", ""
        )
        locator = BackupLocator(fake)
        assert locator.detect_mysql_version("/b") == "5.7.44"

    def test_detect_failure_returns_none(self):
        fake = FakeExecutor()
        fake.run_results["cat /b/xtrabackup_info 2>/dev/null"] = ExecResult(1, "", "no file")
        locator = BackupLocator(fake)
        assert locator.detect_mysql_version("/b") is None


class TestBackupPuller:
    def _make_fake_with_sequence(self, responses: list[ExecResult], record: list):
        """造一个按序返回响应的 fake executor（记录命令）。"""
        fake = FakeExecutor()
        it = iter(responses)

        def _run(cmd, timeout=None):
            record.append(cmd)
            try:
                return next(it)
            except StopIteration:
                return ExecResult(0, "yes\n", "")  # 兜底

        fake.run = _run
        return fake

    def test_same_host_uses_cp(self):
        """同机（源=恢复）用 cp。"""
        cmds: list[str] = []
        # 响应序列：幂等检查(no) → mkdir → cp → 完整性校验(yes)
        fake = self._make_fake_with_sequence([
            ExecResult(0, "no\n", ""),
            ExecResult(0, "", ""),
            ExecResult(0, "", ""),
            ExecResult(0, "yes\n", ""),
        ], cmds)

        puller = BackupPuller(executor=fake, tmp_backup_dir="/tmp/bk")
        dest = puller.pull(
            backup_source_path="/data/backups/db1/latest",
            backup_source_host="192.168.1.15",
            recovery_host="192.168.1.15",   # 同机
            instance_name="db1",
        )
        assert dest == "/tmp/bk/db1_latest"
        assert any("cp -a /data/backups/db1/latest" in c for c in cmds)
        assert not any("scp" in c for c in cmds)

    def test_cross_host_uses_scp(self):
        """跨机用 scp。"""
        cmds: list[str] = []
        # 响应序列：幂等检查(no) → mkdir → scp → 完整性校验(yes)
        fake = self._make_fake_with_sequence([
            ExecResult(0, "no\n", ""),
            ExecResult(0, "", ""),
            ExecResult(0, "", ""),
            ExecResult(0, "yes\n", ""),
        ], cmds)

        puller = BackupPuller(executor=fake, tmp_backup_dir="/tmp/bk")
        dest = puller.pull(
            backup_source_path="/data/backups/db1/latest",
            backup_source_host="10.0.0.200",
            recovery_host="192.168.1.15",   # 跨机
            instance_name="db1",
        )
        assert dest == "/tmp/bk/db1_latest"
        assert any(
            c.startswith("scp -r -o StrictHostKeyChecking=no root@10.0.0.200:/data/backups/db1/latest")
            for c in cmds
        )

    def test_already_pulled_skips(self):
        """已存在完整备份则跳过拉取。"""
        fake = FakeExecutor()
        fake.run_results[
            "test -f /tmp/bk/db1_latest/xtrabackup_checkpoints && echo yes || echo no"
        ] = ExecResult(0, "yes\n", "")
        puller = BackupPuller(executor=fake, tmp_backup_dir="/tmp/bk")
        dest = puller.pull(
            backup_source_path="/data/backups/db1/latest",
            backup_source_host="10.0.0.200",
            recovery_host="192.168.1.15",
            instance_name="db1",
        )
        assert dest == "/tmp/bk/db1_latest"
        # 无 cp/scp 调用
        cmds = [c[1][0] for c in fake.calls if c[0] == "run"]
        assert not any("cp -a" in c or "scp" in c for c in cmds)


class TestXtrabackupBinaryMatrix:
    def test_binary_for_80(self):
        xb = Xtrabackup(
            executor=FakeExecutor(),
            binary_matrix={"8.0": "/usr/bin/xtrabackup", "5.7": "/usr/bin/xtrabackup24"},
        )
        assert xb.binary_for("8.0.35") == "/usr/bin/xtrabackup"
        assert xb.binary_for("8.0.36") == "/usr/bin/xtrabackup"

    def test_binary_for_57(self):
        xb = Xtrabackup(
            executor=FakeExecutor(),
            binary_matrix={"5.7": "/usr/bin/xtrabackup24"},
        )
        assert xb.binary_for("5.7.44") == "/usr/bin/xtrabackup24"

    def test_unmatched_falls_back(self):
        xb = Xtrabackup(executor=FakeExecutor(), binary_path="/usr/bin/xtrabackup")
        assert xb.binary_for("8.0.35") == "/usr/bin/xtrabackup"

    def test_prepare_uses_version_binary(self):
        fake = FakeExecutor()
        fake.run_results[
            "/usr/bin/xtrabackup24 --prepare --target-dir=/tmp/bak57 2>&1"
        ] = ExecResult(0, "completed OK!\n", "")
        xb = Xtrabackup(
            executor=fake, binary_matrix={"5.7": "/usr/bin/xtrabackup24"},
        )
        xb.prepare("/tmp/bak57", mysql_version="5.7.44")
        cmds = [c[1][0] for c in fake.calls if c[0] == "run"]
        assert any(c.startswith("/usr/bin/xtrabackup24 --prepare") for c in cmds)
