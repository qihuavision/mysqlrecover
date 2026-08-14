"""命令执行抽象层（ADR-01 衍生，Sprint 1 地基）。

因采用"管理机 SSH 到恢复机"模式（ADR-01），所有对恢复机的操作
（装 MySQL、跑 xtrabackup、启停容器、scp 备份）都必须经过本抽象层，
禁止在业务模块里散落 subprocess / paramiko 调用。

设计：
- CommandExecutor 是抽象接口，installer/backup/recovery 只依赖它
- LocalExecutor：管理机本地操作（元数据库、报告生成）
- SSHExecutor：远程操作（恢复机、备份源机）
- 单元测试用 FakeExecutor mock，不碰真实 SSH
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ExecResult:
    """命令执行结果。"""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandExecutor(ABC):
    """命令执行抽象基类。本地与远程实现统一此接口。"""

    @abstractmethod
    def run(self, cmd: str, timeout: int | None = None) -> ExecResult:
        """执行 shell 命令，返回 ExecResult。"""

    @abstractmethod
    def put(self, local: str, remote: str) -> None:
        """上传文件到远端（scp）。LocalExecutor 等价 copy。"""

    @abstractmethod
    def get(self, remote: str, local: str) -> None:
        """从远端下载文件（scp）。LocalExecutor 等价 copy。"""

    @abstractmethod
    def file_exists(self, path: str) -> bool:
        """判断远端文件是否存在。"""

    def run_checked(self, cmd: str, timeout: int | None = None) -> ExecResult:
        """执行命令，返回码非 0 抛 RuntimeError。"""
        logger.debug("run_checked: %s", cmd)
        res = self.run(cmd, timeout)
        if not res.ok:
            raise RuntimeError(
                f"命令执行失败 (rc={res.returncode}): {cmd}\nstderr: {res.stderr}"
            )
        return res


class LocalExecutor(CommandExecutor):
    """本地执行（管理机上的元数据库、报告等）。"""

    def run(self, cmd: str, timeout: int | None = None) -> ExecResult:
        logger.debug("local run: %s", cmd)
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return ExecResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    def put(self, local: str, remote: str) -> None:
        """本地等价 copy（local → remote 都是本地路径）。"""
        Path(remote).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local, remote)

    def get(self, remote: str, local: str) -> None:
        """本地等价 copy（remote → local 都是本地路径）。"""
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(remote, local)

    def file_exists(self, path: str) -> bool:
        return Path(path).exists()


class SSHExecutor(CommandExecutor):
    """SSH 远程执行（恢复机、备份源机）。

    基于 paramiko。凭据：SSH 私钥路径从构造参数或环境变量读，不硬编码。
    """

    def __init__(
        self,
        host: str,
        user: str = "root",
        port: int = 22,
        key_path: str | None = None,
        password: str | None = None,
    ):
        self.host = host
        self.user = user
        self.port = port
        # 私钥优先级：显式参数 > 环境变量 DRILL_SSH_KEY > ~/.ssh/id_rsa
        self.key_path = key_path or os.environ.get("DRILL_SSH_KEY") or str(
            Path.home() / ".ssh" / "id_rsa"
        )
        self.password = password
        self._client = None  # paramiko.SSHClient，惰性连接

    def _get_client(self):
        """惰性建立 SSH 连接（复用同一连接）。带重试（Sprint 4 稳定性）。"""
        if self._client is not None:
            return self._client
        import paramiko  # 延迟导入，未装 paramiko 时本地操作不受影响

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs = {
            "hostname": self.host,
            "port": self.port,
            "username": self.user,
            "timeout": 30,
        }
        if Path(self.key_path).exists():
            connect_kwargs["key_filename"] = self.key_path
        elif self.password:
            connect_kwargs["password"] = self.password
        else:
            # 无密钥无密码，尝试默认 agent
            pass

        # 连接重试（3 次，退避 1s/2s/4s）——长时间演练网络抖动容错
        import time
        last_err = None
        for attempt in range(1, 4):
            try:
                logger.debug("SSH connect %s@%s:%s (attempt %d)", self.user, self.host, self.port, attempt)
                client.connect(**connect_kwargs)
                self._client = client
                return client
            except Exception as e:
                last_err = e
                logger.warning("SSH 连接失败（第 %d 次）: %s", attempt, e)
                if attempt < 3:
                    time.sleep(2 ** (attempt - 1))
        raise RuntimeError(f"SSH 连接 {self.user}@{self.host}:{self.port} 失败（重试3次）: {last_err}")

    def _reset_client(self) -> None:
        """重置连接（断线后重建）。"""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None

    def run(self, cmd: str, timeout: int | None = None) -> ExecResult:
        """执行远程命令。连接断开时自动重连一次（Sprint 4 稳定性）。"""
        logger.debug("ssh run [%s]: %s", self.host, cmd)
        try:
            client = self._get_client()
            stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
            exit_code = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            return ExecResult(returncode=exit_code, stdout=out, stderr=err)
        except RuntimeError:
            raise  # 连接失败（已重试3次）直接抛
        except Exception as e:
            # 传输层异常（连接断开等）：重置连接重试一次
            logger.warning("SSH 执行异常（%s），重置连接重试: %s", type(e).__name__, e)
            self._reset_client()
            client = self._get_client()
            stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
            exit_code = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            return ExecResult(returncode=exit_code, stdout=out, stderr=err)

    def put(self, local: str, remote: str) -> None:
        """通过 SFTP 上传文件。"""
        logger.debug("sftp put %s -> %s:%s", local, self.host, remote)
        client = self._get_client()
        sftp = client.open_sftp()
        try:
            # 确保远程父目录存在
            remote_dir = str(Path(remote).parent)
            self.run(f"mkdir -p {remote_dir}")
            sftp.put(local, remote)
        finally:
            sftp.close()

    def get(self, remote: str, local: str) -> None:
        """通过 SFTP 下载文件。"""
        logger.debug("sftp get %s:%s -> %s", self.host, remote, local)
        client = self._get_client()
        sftp = client.open_sftp()
        try:
            Path(local).parent.mkdir(parents=True, exist_ok=True)
            sftp.get(remote, local)
        finally:
            sftp.close()

    def file_exists(self, path: str) -> bool:
        res = self.run(f"test -e {path} && echo yes || echo no")
        return res.stdout.strip() == "yes"

    def close(self) -> None:
        """关闭 SSH 连接。"""
        if self._client is not None:
            self._client.close()
            self._client = None


class FakeExecutor(CommandExecutor):
    """测试用假执行器。记录所有调用，可预设返回结果。

    用法：
        fake = FakeExecutor()
        fake.run_results["ls"] = ExecResult(0, "file1\n", "")
        assert fake.run("ls").ok
        assert fake.calls[0] == ("run", "ls")
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.run_results: dict[str, ExecResult] = {}
        self.files: set[str] = set()

    def run(self, cmd: str, timeout: int | None = None) -> ExecResult:
        self.calls.append(("run", (cmd,)))
        # 精确匹配优先，否则返回默认成功
        return self.run_results.get(cmd, ExecResult(0, "", ""))

    def put(self, local: str, remote: str) -> None:
        self.calls.append(("put", (local, remote)))

    def get(self, remote: str, local: str) -> None:
        self.calls.append(("get", (remote, local)))

    def file_exists(self, path: str) -> bool:
        self.calls.append(("file_exists", (path,)))
        return path in self.files
