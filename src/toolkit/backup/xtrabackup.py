"""xtrabackup 命令封装（FP-03 恢复流程核心工具）。

封装 prepare / copy-back 两个关键操作，经 executor 在恢复机执行。
统一日志捕获到文件（供验证步骤 grep 'completed OK!'）。

ADR-09：xtrabackup 在宿主机执行（不在容器内），datadir 挂载到容器。
密码经环境变量 MYSQL_PWD 注入（ADR-10），不进命令行。
"""
from __future__ import annotations

import logging

from toolkit.core.exceptions import RecoveryError
from toolkit.core.executor import CommandExecutor
from toolkit.core.logger import get_logger

logger = get_logger(__name__)


class Xtrabackup:
    """xtrabackup 命令封装（经 executor 在恢复机执行）。"""

    def __init__(self, executor: CommandExecutor, binary_path: str = "/usr/bin/xtrabackup"):
        self.executor = executor
        self.binary = binary_path

    def prepare(self, backup_path: str, log_file: str | None = None) -> str:
        """xtrabackup --prepare --target-dir=<backup_path>

        Args:
            backup_path: 备份目录（恢复机本地路径）
            log_file: 日志输出路径（供验证 grep completed OK!）

        Returns:
            日志内容（或写入 log_file）

        Raises:
            RecoveryError: prepare 失败
        """
        # 注意：用 2>&1 重定向而非 tee 管道（tee 会覆盖 exit code）
        if log_file:
            log_redirect = f" >{log_file} 2>&1"
        else:
            log_redirect = " 2>&1"
        cmd = f"{self.binary} --prepare --target-dir={backup_path}{log_redirect}"
        logger.info("xtrabackup prepare: %s", backup_path)
        res = self.executor.run(cmd, timeout=3600)
        # 即使有重定向，stdout 仍可能捕获输出（取决于 executor 实现）
        log_content = res.stdout or ""
        # 如果 stdout 空（被重定向了），尝试读日志文件
        if not log_content and log_file:
            read_res = self.executor.run(f"cat {log_file} 2>/dev/null")
            log_content = read_res.stdout
        if "completed OK!" not in log_content:
            raise RecoveryError(
                f"xtrabackup prepare 未输出 'completed OK!'，可能未成功。"
                f"日志末尾: {log_content[-500:]}"
            )
        return log_content

    def copy_back(self, backup_path: str, datadir: str, log_file: str | None = None) -> str:
        """xtrabackup --copy-back --target-dir=<backup> --datadir=<datadir>

        前置条件：MySQL 已停止、datadir 已清空。

        Raises:
            RecoveryError: copy-back 失败
        """
        if log_file:
            log_redirect = f" >{log_file} 2>&1"
        else:
            log_redirect = " 2>&1"
        cmd = (
            f"{self.binary} --copy-back "
            f"--target-dir={backup_path} "
            f"--datadir={datadir}{log_redirect}"
        )
        logger.info("xtrabackup copy-back -> %s", datadir)
        res = self.executor.run(cmd, timeout=3600)
        log_content = res.stdout or ""
        if not log_content and log_file:
            read_res = self.executor.run(f"cat {log_file} 2>/dev/null")
            log_content = read_res.stdout
        if "completed OK!" not in log_content:
            raise RecoveryError(
                f"xtrabackup copy-back 未输出 'completed OK!'。日志末尾: {log_content[-500:]}"
            )
        return log_content

    def version(self) -> str:
        """获取 xtrabackup 版本。"""
        res = self.executor.run(f"{self.binary} --version 2>&1")
        return res.stdout.strip() if res.ok else "unknown"

    @staticmethod
    def check_log_ok(log_content: str) -> bool:
        """检查日志是否含 'completed OK!'。"""
        return "completed OK!" in log_content
