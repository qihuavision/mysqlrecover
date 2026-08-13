"""RecoveryTaskRunner 单元测试（mock installer/xtrabackup/verifier）。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from toolkit.core.db import init_db, reset_db
from toolkit.core.executor import FakeExecutor
from toolkit.recovery.task import RecoveryTaskRunner
from toolkit.recovery.verifier import VerifyResult


@pytest.fixture
def runner():
    """构造一个用 mock 组件的 RecoveryTaskRunner。"""
    init_db("sqlite:///:memory:")
    fake_executor = FakeExecutor()

    installer = MagicMock()
    installer.container_name.return_value = "drill-mysql-8035"
    installer.datadir.return_value = "/data/drill/8.0.35/datadir"
    installer.list_containers.return_value = []  # 无 running 容器

    xtrabackup = MagicMock()
    xtrabackup.prepare.return_value = "completed OK!"
    xtrabackup.copy_back.return_value = "completed OK!"

    verifier = MagicMock()

    r = RecoveryTaskRunner(
        executor=fake_executor,
        installer=installer,
        xtrabackup=xtrabackup,
        verifier=verifier,
        archive_root="/tmp/archive",
    )
    yield r, installer, xtrabackup, verifier, fake_executor
    reset_db()


class TestRecoveryTaskDryRun:
    def test_dry_run_returns_success_without_side_effects(self, runner):
        r, installer, xtrabackup, verifier, _ = runner
        result = r.execute(
            mysql_version="8.0.35",
            backup_remote_path="/tmp/bak",
            instance_name="test-db",
            backup_source_host="10.0.0.9",
            dry_run=True,
        )
        assert result.success is True
        assert "dry-run" in result.error_msg
        # 不应调用真实操作
        installer.ensure_container.assert_not_called()
        xtrabackup.prepare.assert_not_called()


class TestRecoveryTaskSuccess:
    def test_full_success_flow(self, runner):
        r, installer, xtrabackup, verifier, _ = runner
        verifier.verify.return_value = VerifyResult(
            passed=True, detail="ok", verify_db="orders",
            verify_table="order_info", verify_count=42,
        )

        result = r.execute(
            mysql_version="8.0.35",
            backup_remote_path="/tmp/bak",
            instance_name="test-db",
            backup_source_host="10.0.0.9",
        )

        assert result.success is True
        assert result.container_name == "drill-mysql-8035"
        assert result.verify_count == 42
        # 验证调用顺序：ensure → start → stop → clean → prepare → copyback → chown → start → verify
        installer.ensure_container.assert_called_once_with("8.0.35")
        installer.start.assert_called_with("8.0.35")
        installer.clean_datadir.assert_called_once_with("8.0.35")
        installer.chown_datadir.assert_called_once_with("8.0.35")
        xtrabackup.prepare.assert_called_once()
        xtrabackup.copy_back.assert_called_once()
        verifier.verify.assert_called_once()

    def test_stops_other_running_containers(self, runner):
        """步骤 2：应停止其他 running 容器。"""
        r, installer, xtrabackup, verifier, _ = runner
        # 模拟有另一个 running 容器
        from toolkit.installer.docker import ContainerInfo
        installer.list_containers.return_value = [
            ContainerInfo(name="drill-mysql-5744", version="5.7.44", status="running", exists=True),
        ]
        verifier.verify.return_value = VerifyResult(passed=True, detail="ok")

        r.execute(
            mysql_version="8.0.35",
            backup_remote_path="/tmp/bak",
            instance_name="db1",
            backup_source_host="10.0.0.9",
        )
        installer.stop_by_name.assert_called_with("drill-mysql-5744")


class TestRecoveryTaskFailure:
    def test_prepare_failure_collected(self, runner):
        """prepare 失败时应捕获错误日志。"""
        from toolkit.core.exceptions import RecoveryError
        r, installer, xtrabackup, verifier, _ = runner
        xtrabackup.prepare.side_effect = RecoveryError("prepare failed")

        result = r.execute(
            mysql_version="8.0.35",
            backup_remote_path="/tmp/bak",
            instance_name="db1",
            backup_source_host="10.0.0.9",
        )

        assert result.success is False
        assert "prepare failed" in result.error_msg

    def test_verify_failure_collected(self, runner):
        """验证失败时应采集 docker logs。"""
        r, installer, xtrabackup, verifier, _ = runner
        verifier.verify.return_value = VerifyResult(
            passed=False, detail="COUNT=0", verify_count=0
        )
        installer.get_docker_logs.return_value = "some docker log"

        result = r.execute(
            mysql_version="8.0.35",
            backup_remote_path="/tmp/bak",
            instance_name="db1",
            backup_source_host="10.0.0.9",
        )

        assert result.success is False
        assert "COUNT=0" in result.error_msg
        installer.get_docker_logs.assert_called()
