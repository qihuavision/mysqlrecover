"""DockerInstaller 单元测试（用 FakeExecutor，不碰真实 Docker）。"""
from __future__ import annotations

import pytest

from toolkit.core.db import init_db, reset_db
from toolkit.core.exceptions import InstallError
from toolkit.core.executor import ExecResult, FakeExecutor
from toolkit.installer.docker import DockerInstaller


@pytest.fixture
def fake_executor():
    return FakeExecutor()


@pytest.fixture
def installer(fake_executor):
    init_db("sqlite:///:memory:")
    yield DockerInstaller(
        executor=fake_executor,
        drill_port=13306,
        datadir_base="/data/drill",
        supported_versions=["8.0.35", "5.7.44"],
    )
    reset_db()


class TestContainerNameAndPaths:
    def test_container_name(self, installer):
        assert installer.container_name("8.0.35") == "drill-mysql-8035"
        assert installer.container_name("5.7.44") == "drill-mysql-5744"

    def test_datadir(self, installer):
        assert installer.datadir("8.0.35") == "/data/drill/8.0.35/datadir"

    def test_image(self, installer):
        assert installer.image("8.0.35") == "mysql:8.0.35"


class TestEnsureContainer:
    def test_creates_new_container(self, installer, fake_executor):
        """容器不存在时应创建。"""
        # docker inspect 返回空（不存在）
        fake_executor.run_results[
            "docker inspect --format '{{.State.Status}}' drill-mysql-8035 2>/dev/null"
        ] = ExecResult(1, "", "not found")
        # docker run 成功
        run_cmd_prefix = "docker run -d --network host --name drill-mysql-8035"
        for key in list(fake_executor.run_results):
            if key.startswith("docker run"):
                del fake_executor.run_results[key]

        name = installer.ensure_container("8.0.35")
        assert name == "drill-mysql-8035"
        # 应该执行了 mkdir、docker run、docker stop
        run_calls = [c for c in fake_executor.calls if c[0] == "run"]
        cmds = [c[1][0] for c in run_calls]
        assert any("mkdir -p /data/drill/8.0.35/datadir" in c for c in cmds)
        assert any("docker run -d" in c and "drill-mysql-8035" in c for c in cmds)
        assert any("docker stop drill-mysql-8035" in c for c in cmds)

    def test_skips_existing_container(self, installer, fake_executor):
        """容器已存在时应跳过创建。"""
        inspect_cmd = (
            "docker inspect --format '{{.State.Status}}' drill-mysql-8035 2>/dev/null"
        )
        fake_executor.run_results[inspect_cmd] = ExecResult(0, "exited\n", "")

        name = installer.ensure_container("8.0.35")
        assert name == "drill-mysql-8035"
        # 不应执行 docker run
        run_calls = [c for c in fake_executor.calls if c[0] == "run"]
        cmds = [c[1][0] for c in run_calls]
        assert not any("docker run" in c for c in cmds)

    def test_dry_run_no_side_effects(self, installer, fake_executor):
        """dry-run 模式不应真正创建。"""
        inspect_cmd = (
            "docker inspect --format '{{.State.Status}}' drill-mysql-8035 2>/dev/null"
        )
        fake_executor.run_results[inspect_cmd] = ExecResult(1, "", "")

        name = installer.ensure_container("8.0.35", dry_run=True)
        assert name == "drill-mysql-8035"
        run_calls = [c for c in fake_executor.calls if c[0] == "run"]
        cmds = [c[1][0] for c in run_calls]
        assert not any("docker run" in c for c in cmds)

    def test_unsupported_version_raises(self, installer):
        with pytest.raises(InstallError, match="不在支持列表"):
            installer.ensure_container("9.9.99")


class TestStartStop:
    def test_start(self, installer, fake_executor):
        fake_executor.run_results["docker start drill-mysql-8035"] = ExecResult(0, "drill-mysql-8035\n", "")
        installer.start("8.0.35")
        assert any("docker start" in c[1][0] for c in fake_executor.calls if c[0] == "run")

    def test_start_failure_raises(self, installer, fake_executor):
        fake_executor.run_results["docker start drill-mysql-8035"] = ExecResult(1, "", "no such container")
        with pytest.raises(InstallError, match="失败"):
            installer.start("8.0.35")

    def test_stop(self, installer, fake_executor):
        installer.stop("8.0.35")
        assert any("docker stop" in c[1][0] for c in fake_executor.calls if c[0] == "run")

    def test_stop_nonexistent_no_raise(self, installer, fake_executor):
        """停止不存在的容器不应抛异常（可能已停止）。"""
        fake_executor.run_results["docker stop drill-mysql-8035"] = ExecResult(1, "", "")
        installer.stop("8.0.35")  # 不应抛异常


class TestInspect:
    def test_running_container(self, installer, fake_executor):
        inspect_cmd = (
            "docker inspect --format '{{.State.Status}}' drill-mysql-8035 2>/dev/null"
        )
        fake_executor.run_results[inspect_cmd] = ExecResult(0, "running\n", "")
        info = installer.inspect("drill-mysql-8035")
        assert info.exists is True
        assert info.status == "running"

    def test_stopped_container(self, installer, fake_executor):
        inspect_cmd = (
            "docker inspect --format '{{.State.Status}}' drill-mysql-8035 2>/dev/null"
        )
        fake_executor.run_results[inspect_cmd] = ExecResult(0, "exited\n", "")
        info = installer.inspect("drill-mysql-8035")
        assert info.exists is True
        assert info.status == "stopped"

    def test_not_found(self, installer, fake_executor):
        inspect_cmd = (
            "docker inspect --format '{{.State.Status}}' nonexistent 2>/dev/null"
        )
        fake_executor.run_results[inspect_cmd] = ExecResult(1, "", "")
        info = installer.inspect("nonexistent")
        assert info.exists is False
        assert info.status == "not_found"


class TestDatadirOps:
    def test_clean_datadir(self, installer, fake_executor):
        installer.clean_datadir("8.0.35")
        cmds = [c[1][0] for c in fake_executor.calls if c[0] == "run"]
        assert any("rm -rf" in c and "/data/drill/8.0.35/datadir" in c for c in cmds)

    def test_chown_datadir(self, installer, fake_executor):
        installer.chown_datadir("8.0.35")
        cmds = [c[1][0] for c in fake_executor.calls if c[0] == "run"]
        assert any("chown -R 999:999" in c for c in cmds)


class TestGetDockerLogs:
    def test_returns_logs(self, installer, fake_executor):
        fake_executor.run_results["docker logs drill-mysql-8035 2>&1"] = ExecResult(
            0, "some error log\n", ""
        )
        logs = installer.get_docker_logs("8.0.35")
        assert "some error log" in logs

    def test_returns_error_on_failure(self, installer, fake_executor):
        fake_executor.run_results["docker logs drill-mysql-8035 2>&1"] = ExecResult(
            1, "", "failed"
        )
        logs = installer.get_docker_logs("8.0.35")
        assert "获取日志失败" in logs
