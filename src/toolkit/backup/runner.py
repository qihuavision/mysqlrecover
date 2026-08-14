"""备份执行（FP-02, ADR-06 接管模式, Sprint 3 实现）。

SSH 到源实例执行 xtrabackup --backup，备份产物落到备份源机。
备份完成后登记到 backups 表。

设计（参考 pyxtrabackup 流水线，ADR-08）：
1. 前置检查（磁盘空间、xtrabackup 存在）
2. xtrabackup --backup（远程执行）
3. 校验产物完整性（xtrabackup_checkpoints）
4. 登记到元数据库
"""
from __future__ import annotations

from datetime import datetime, timezone

from toolkit.core.db import get_session_ctx
from toolkit.core.exceptions import BackupNotFoundError, RecoveryError
from toolkit.core.executor import CommandExecutor
from toolkit.core.logger import get_logger
from toolkit.core.models import Backup, Instance

logger = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class BackupRunner:
    """备份执行器（Sprint 3：接管 crontab 备份）。"""

    def __init__(
        self,
        executor: CommandExecutor,  # 连到源实例所在机器的 SSHExecutor
        xtrabackup_path: str = "/usr/bin/xtrabackup",
        backup_user: str = "root",
        password_env: str = "DRILL_MYSQL_PWD",
        mysql_port: int = 3306,
        min_free_gb: int = 10,
        datadir: str = "",  # 容器化 MySQL 的宿主机挂载 datadir（如 /data/drill/8.0.35/datadir）
    ):
        self.executor = executor
        self.xtrabackup_path = xtrabackup_path
        self.backup_user = backup_user
        self.password_env = password_env
        self.mysql_port = mysql_port
        self.min_free_gb = min_free_gb
        self.datadir = datadir

    # ---------- 登记已有备份（Sprint 1 遗留接口）----------

    def register_existing(self, backup_path: str, instance_id: int) -> int:
        """登记一个已有备份到 backups 表。返回 backup_id。"""
        session = get_session_ctx()
        try:
            record = Backup(
                instance_id=instance_id,
                backup_path=backup_path,
                status="available",
                discovered_at=_now_iso(),
            )
            session.add(record)
            session.commit()
            return record.id
        finally:
            session.close()

    # ---------- 触发新备份（Sprint 3 核心）----------

    def run_backup(self, instance: Instance, backup_root: str, dry_run: bool = False) -> int:
        """触发一次 xtrabackup 备份并登记。

        在源实例机器上执行（executor 已连到该机器）：
        xtrabackup --backup --target-dir={backup_root}/{name}/{timestamp}

        Args:
            instance: ORM Instance（源实例）
            backup_root: 备份落盘根目录（源机本地）
            dry_run: 只打印

        Returns:
            backup_id（登记后的 ID）

        Raises:
            RecoveryError: 备份失败
        """
        import os

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target_dir = f"{backup_root}/{instance.name}/{timestamp}"

        if dry_run:
            logger.info("[dry-run] 将备份 %s -> %s", instance.name, target_dir)
            return 0

        logger.info("开始备份 %s -> %s", instance.name, target_dir)

        # 1. 前置检查：磁盘空间 + 创建目录
        self._check_disk(backup_root)
        self.executor.run_checked(f"mkdir -p {target_dir}")

        # 2. 执行备份。
        # 密码经临时 defaults 文件传给 xtrabackup（8.0 不再读 MYSQL_PWD 环境变量），
        # 文件权限 600，备份后立即删除（ADR-10：密码不进命令行/ps）
        password = os.environ.get(self.password_env, "")
        creds_file = f"/tmp/.xb_creds_{instance.name}_{timestamp}"
        write_creds = (
            f"umask 177 && printf '[client]\\nuser={self.backup_user}\\npassword={password}\\n' "
            f"> {creds_file}"
        )
        self.executor.run_checked(write_creds)

        try:
            # --defaults-extra-file 必须是第一个参数（xtrabackup 要求）
            # --datadir：容器化 MySQL 的 datadir 是宿主机挂载路径（容器内路径宿主看不到）
            datadir_arg = f"--datadir={self.datadir} " if self.datadir else ""
            cmd = (
                f"{self.xtrabackup_path} "
                f"--defaults-extra-file={creds_file} "
                f"--backup "
                f"--host=127.0.0.1 --port={self.mysql_port} "
                f"{datadir_arg}"
                f"--target-dir={target_dir} --no-lock 2>&1"
            )
            res = self.executor.run(cmd, timeout=7200)
            if not res.ok or "completed OK!" not in (res.stdout or ""):
                raise RecoveryError(
                    f"备份 {instance.name} 失败: {(res.stdout or res.stderr)[-500:]}"
                )
        finally:
            # 无论成败都删凭据文件
            self.executor.run(f"rm -f {creds_file}")
        logger.info("备份完成: %s", target_dir)

        # 3. 校验完整性
        check = self.executor.run(f"test -f {target_dir}/xtrabackup_checkpoints && echo yes || echo no")
        if check.stdout.strip() != "yes":
            raise RecoveryError(f"备份产物不完整（缺 xtrabackup_checkpoints）: {target_dir}")

        # 4. 登记到数据库
        size = self._dir_size(target_dir)
        session = get_session_ctx()
        try:
            record = Backup(
                instance_id=instance.id,
                backup_path=target_dir,
                status="available",
                size_bytes=size,
                started_at=_now_iso(),
                finished_at=_now_iso(),
            )
            session.add(record)
            session.commit()
            logger.info("已登记备份 #%d: %s (%d bytes)", record.id, target_dir, size)
            return record.id
        finally:
            session.close()

    # ---------- 内部 ----------

    def _check_disk(self, path: str) -> None:
        """检查目标路径磁盘剩余空间。"""
        res = self.executor.run(f"df -BG --output=avail {path} 2>/dev/null | tail -1 | tr -dc '0-9'")
        if res.ok and res.stdout.strip().isdigit():
            free_gb = int(res.stdout.strip())
            if free_gb < self.min_free_gb:
                from toolkit.core.exceptions import DiskFullError
                raise DiskFullError(
                    f"磁盘空间不足: {path} 剩余 {free_gb}G < 要求 {self.min_free_gb}G"
                )

    def _dir_size(self, path: str) -> int:
        """目录大小（字节）。"""
        res = self.executor.run(f"du -sb {path} 2>/dev/null | cut -f1")
        if res.ok and res.stdout.strip().isdigit():
            return int(res.stdout.strip())
        return 0
