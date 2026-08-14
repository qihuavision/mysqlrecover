"""备份拉取（FP-02, ADR-04 scp 模式, Sprint 5 实现）。

把备份从备份源机拉到恢复机临时目录：
- 跨机场景：在恢复机上执行 scp（要求恢复机→源机 SSH 免密，运维常规）
- 同机场景（源机=恢复机）：直接 cp，零开销

拉取目标：{tmp_backup_dir}/{实例名}_{时间戳}/
"""
from __future__ import annotations

from datetime import datetime

from toolkit.core.exceptions import RecoveryError
from toolkit.core.executor import CommandExecutor
from toolkit.core.logger import get_logger

logger = get_logger(__name__)


class BackupPuller:
    """备份拉取器（源机 → 恢复机）。"""

    def __init__(
        self,
        executor: CommandExecutor,  # 连到恢复机的 SSHExecutor
        tmp_backup_dir: str = "/data/drill/tmp-backups",
        source_ssh_user: str = "root",
    ):
        self.executor = executor
        self.tmp_backup_dir = tmp_backup_dir
        self.source_ssh_user = source_ssh_user

    def pull(
        self,
        backup_source_path: str,      # 备份在源机上的路径
        backup_source_host: str,      # 备份源机 IP
        recovery_host: str,           # 恢复机 IP（判断同机）
        instance_name: str = "",
    ) -> str:
        """拉取备份，返回恢复机本地路径。

        幂等：目标已存在则跳过拉取。
        """
        # 目标路径：{tmp}/{实例名}_{备份basename}
        src_name = backup_source_path.rstrip("/").split("/")[-1]
        dest = f"{self.tmp_backup_dir}/{instance_name}_{src_name}" if instance_name \
            else f"{self.tmp_backup_dir}/{src_name}"

        # 幂等检查：目标存在且完整（有 checkpoints）则跳过
        check = self.executor.run(
            f"test -f {dest}/xtrabackup_checkpoints && echo yes || echo no"
        )
        if check.stdout.strip() == "yes":
            logger.info("备份已存在于恢复机: %s，跳过拉取", dest)
            return dest

        self.executor.run_checked(f"mkdir -p {self.tmp_backup_dir}")

        # 同机场景：直接 cp
        if backup_source_host == recovery_host or backup_source_host in ("127.0.0.1", "localhost"):
            logger.info("同机拉取（cp）: %s -> %s", backup_source_path, dest)
            cmd = f"cp -a {backup_source_path} {dest}"
        else:
            # 跨机：恢复机主动 scp（要求恢复机→源机免密）
            logger.info("跨机拉取（scp %s:%s）", backup_source_host, backup_source_path)
            cmd = (
                f"scp -r -o StrictHostKeyChecking=no "
                f"{self.source_ssh_user}@{backup_source_host}:{backup_source_path} {dest}"
            )

        res = self.executor.run(cmd, timeout=3600)
        if not res.ok:
            raise RecoveryError(
                f"备份拉取失败 ({backup_source_host}:{backup_source_path} -> {dest}): "
                f"{res.stderr[-300:]}"
            )

        # 完整性校验
        verify = self.executor.run(
            f"test -f {dest}/xtrabackup_checkpoints && echo yes || echo no"
        )
        if verify.stdout.strip() != "yes":
            raise RecoveryError(f"拉取的备份不完整（缺 xtrabackup_checkpoints）: {dest}")

        logger.info("备份拉取完成: %s", dest)
        return dest
