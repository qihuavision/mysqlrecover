"""executor.py 单元测试。

测试 LocalExecutor（真实本地执行）和 FakeExecutor（测试桩）。
SSHExecutor 需要真实 SSH，放集成测试。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from toolkit.core.executor import (
    CommandExecutor,
    ExecResult,
    FakeExecutor,
    LocalExecutor,
)


class TestExecResult:
    def test_ok_when_returncode_zero(self):
        assert ExecResult(0, "out", "err").ok is True

    def test_not_ok_when_returncode_nonzero(self):
        assert ExecResult(1, "", "err").ok is False


class TestLocalExecutor:
    def test_run_success(self):
        ex = LocalExecutor()
        res = ex.run("echo hello")
        assert res.returncode == 0
        assert res.stdout.strip() == "hello"

    def test_run_failure(self):
        ex = LocalExecutor()
        res = ex.run("exit 3")
        assert res.returncode == 3
        assert res.ok is False

    def test_run_checked_raises_on_failure(self):
        ex = LocalExecutor()
        with pytest.raises(RuntimeError, match="命令执行失败"):
            ex.run_checked("exit 1")

    def test_run_checked_returns_on_success(self):
        ex = LocalExecutor()
        res = ex.run_checked("echo ok")
        assert res.ok

    def test_file_exists_true(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("x")
        assert LocalExecutor().file_exists(str(f)) is True

    def test_file_exists_false(self, tmp_path):
        assert LocalExecutor().file_exists(str(tmp_path / "nope.txt")) is False

    def test_put_copies_file(self, tmp_path):
        src = tmp_path / "src.txt"
        src.write_text("data")
        dst = tmp_path / "sub" / "dst.txt"
        LocalExecutor().put(str(src), str(dst))
        assert dst.read_text() == "data"

    def test_get_copies_file(self, tmp_path):
        src = tmp_path / "src.txt"
        src.write_text("data")
        dst = tmp_path / "sub" / "dst.txt"
        LocalExecutor().get(str(src), str(dst))
        assert dst.read_text() == "data"

    def test_run_timeout(self):
        ex = LocalExecutor()
        # timeout 1 秒，命令要 10 秒 → 应超时抛 subprocess.TimeoutExpired
        with pytest.raises(Exception):
            ex.run("ping -n 10 127.0.0.1 > nul" if os.name == "nt" else "sleep 10", timeout=1)


class TestFakeExecutor:
    def test_run_default_success(self):
        fake = FakeExecutor()
        assert fake.run("anything").ok

    def test_run_preset_result(self):
        fake = FakeExecutor()
        fake.run_results["ls -la"] = ExecResult(0, "file1\n", "")
        res = fake.run("ls -la")
        assert res.stdout == "file1\n"

    def test_calls_recorded(self):
        fake = FakeExecutor()
        fake.files.add("/exists")
        fake.run("cmd1")
        fake.put("a", "b")
        fake.get("c", "d")
        fake.file_exists("/exists")
        assert fake.calls == [
            ("run", ("cmd1",)),
            ("put", ("a", "b")),
            ("get", ("c", "d")),
            ("file_exists", ("/exists",)),
        ]

    def test_file_exists_in_files_set(self):
        fake = FakeExecutor()
        fake.files.add("/some/path")
        assert fake.file_exists("/some/path") is True
        assert fake.file_exists("/other") is False

    def test_implements_interface(self):
        """FakeExecutor 必须是 CommandExecutor 子类（依赖注入兼容）。"""
        assert isinstance(FakeExecutor(), CommandExecutor)
