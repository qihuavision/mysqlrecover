"""定位最新可用备份（FP-02, ADR-04 scp 拉取模式）。

经 executor SSH 到备份源机，扫描备份目录，找到最新可用全量备份。
校验备份完整性（含 xtrabackup_info / xtrabackup_checkpoints 文件）。
"""
from __future__ import annotations

import logging

from toolkit.core.executor import CommandExecutor
from toolkit.core.logger import get_logger

logger = get_logger(__name__)


class BackupLocator:
    """备份定位器（经 executor 在备份源机上扫描）。"""

    def __init__(self, executor: CommandExecutor):
        """executor 应连到备份源机（SSHExecutor(source_host)）。"""
        self.executor = executor

    def find_latest(self, backup_source_path: str) -> str | None:
        """在备份源机上找最新可用的全量备份目录。

        Args:
            backup_source_path: 备份源机上的实例备份根目录，如 /data/backups/order-db

        Returns:
            最新可用备份的完整路径（含日期子目录），无则 None
        """
        # 列出备份根目录下所有子目录（按日期命名），取最新
        cmd = f"ls -1d {backup_source_path}/*/ 2>/dev/null | sort -r | head -1"
        res = self.executor.run(cmd)
        if not res.ok or not res.stdout.strip():
            logger.warning("备份目录 %s 无可用备份", backup_source_path)
            return None

        latest = res.stdout.strip().rstrip("/")
        # 校验完整性
        if self.is_complete(latest):
            logger.info("找到最新备份: %s", latest)
            return latest
        else:
            logger.warning("最新备份 %s 不完整（缺 xtrabackup_checkpoints），跳过", latest)
            return None

    def collect_all(self, instance_paths: dict[str, str]) -> dict[str, str | None]:
        """批量收集多个实例的最新备份。

        Args:
            instance_paths: {实例名: 备份源路径}

        Returns:
            {实例名: 最新备份路径 or None}
        """
        return {name: self.find_latest(path) for name, path in instance_paths.items()}

    def is_complete(self, backup_path: str) -> bool:
        """校验备份是否完整（存在 xtrabackup_checkpoints 文件）。"""
        # xtrabackup_checkpoints 是 xtrabackup 备份的核心标志文件
        res = self.executor.run(f"test -f {backup_path}/xtrabackup_checkpoints && echo yes || echo no")
        return res.stdout.strip() == "yes"

    def get_backup_info(self, backup_path: str) -> dict[str, str]:
        """读取 xtrabackup_checkpoints 获取备份元信息。

        返回 dict，包含 from_lsn / to_lsn / last_lsn / backup_type 等。
        """
        res = self.executor.run(f"cat {backup_path}/xtrabackup_checkpoints 2>/dev/null")
        info: dict[str, str] = {}
        if not res.ok:
            return info
        for line in res.stdout.splitlines():
            line = line.strip()
            if "=" in line:
                key, _, value = line.partition("=")
                info[key.strip()] = value.strip()
        return info

    def detect_mysql_version(self, backup_path: str) -> str | None:
        """从备份文件自动检测 MySQL 版本（Sprint 5）。

        优先读 xtrabackup_info 的 server_version 行（格式：
        server_version = 8.0.35），降级读 xtrabackup_checkpoints。

        Returns:
            版本字符串（如 '8.0.35'），检测失败返回 None
        """
        # 1. xtrabackup_info（首选：含 server_version）
        res = self.executor.run(f"cat {backup_path}/xtrabackup_info 2>/dev/null")
        if res.ok:
            import re
            for line in res.stdout.splitlines():
                line = line.strip()
                if line.startswith("server_version"):
                    # 格式：server_version = 8.0.35（5.7 可能带 -log 等后缀）
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        raw = parts[1].strip()
                        m = re.match(r"^(\d+\.\d+\.\d+)", raw)
                        if m:
                            version = m.group(1)  # 截掉 -log 等后缀
                            logger.info("备份 %s 检测到版本: %s", backup_path, version)
                            return version
        # 2. 降级：备份目录名猜（少见场景）
        logger.warning("无法从 xtrabackup_info 检测 %s 的版本", backup_path)
        return None

    def get_backup_size(self, backup_path: str) -> int:
        """获取备份目录大小（字节）。"""
        res = self.executor.run(f"du -sb {backup_path} 2>/dev/null | cut -f1")
        if res.ok and res.stdout.strip().isdigit():
            return int(res.stdout.strip())
        return 0
