"""单次恢复任务执行（FP-03, ADR-09 的 12 步流程）。

编排 DockerInstaller + Xtrabackup + Verifier，完成单实例恢复演练：
1. 确保目标版本常驻容器存在
2. 停其他版本容器（串行保证）
3. 启目标版本容器
4. 停目标版本容器（copy-back 前置）
5. 清空 datadir
6. xtrabackup --prepare
7. xtrabackup --copy-back
8. chown 999:999 datadir
9. 启动目标版本容器
10. 连 13306 验证（ADR-10 自动发现）
11. 验证通过 → 归档日志
12. 取下一个任务
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from toolkit.backup.xtrabackup import Xtrabackup
from toolkit.core.exceptions import RecoveryError, VerifyError
from toolkit.core.executor import CommandExecutor
from toolkit.core.logger import get_logger
from toolkit.installer.docker import DockerInstaller
from toolkit.recovery.verifier import VerifyResult, Verifier

logger = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class TaskResult:
    """单实例恢复任务结果。"""

    success: bool = False
    error_msg: str = ""
    duration_sec: int = 0
    verify_result: VerifyResult | None = None
    container_name: str = ""
    logs: dict = field(default_factory=dict)  # 各阶段日志路径

    @property
    def verify_db(self) -> str:
        return self.verify_result.verify_db if self.verify_result else ""

    @property
    def verify_table(self) -> str:
        return self.verify_result.verify_table if self.verify_result else ""

    @property
    def verify_count(self) -> int:
        return self.verify_result.verify_count if self.verify_result else -1


class RecoveryTaskRunner:
    """执行单个实例的恢复演练（ADR-09 12 步流程）。

    所有操作经 executor（SSHExecutor，连到恢复机）执行。
    """

    def __init__(
        self,
        executor: CommandExecutor,  # 连到恢复机的 SSHExecutor
        installer: DockerInstaller,
        xtrabackup: Xtrabackup,
        verifier: Verifier,
        archive_root: str = "/data/archive",
        tmp_backup_dir: str = "/data/drill/tmp-backups",
    ):
        self.executor = executor
        self.installer = installer
        self.xtrabackup = xtrabackup
        self.verifier = verifier
        self.archive_root = archive_root
        self.tmp_backup_dir = tmp_backup_dir

    def execute(
        self,
        mysql_version: str,
        backup_remote_path: str,
        instance_name: str,
        backup_source_host: str,
        source_executor: CommandExecutor | None = None,
        dry_run: bool = False,
    ) -> TaskResult:
        """执行单实例恢复演练（ADR-09 12 步）。

        Args:
            mysql_version: 该实例的 MySQL 版本（决定用哪个常驻容器）
            backup_remote_path: 备份在恢复机本地的路径（已 scp 过来）
            instance_name: 实例名（归档目录用）
            backup_source_host: 备份源机 IP（归档目录用）
            source_executor: 备份源机 executor（用于 scp，Sprint 1 可选）

        Returns:
            TaskResult
        """
        start = datetime.now(timezone.utc)
        container = self.installer.container_name(mysql_version)
        date_str = datetime.now().strftime("%Y-%m-%d")
        log_dir = f"{self.archive_root}/{date_str}/{instance_name}_{mysql_version}"

        result = TaskResult(container_name=container)

        try:
            if dry_run:
                return self._dry_run(mysql_version, backup_remote_path, instance_name, container)

            # 归档目录
            self.executor.run_checked(f"mkdir -p {log_dir}")

            # 步骤 1：确保目标版本容器存在
            logger.info("[%s] 步骤1: 确保容器 %s", instance_name, container)
            self.installer.ensure_container(mysql_version)

            # 步骤 2：停止所有 running 的常驻容器（串行保证）
            logger.info("[%s] 步骤2: 停止其他容器", instance_name)
            for c in self.installer.list_containers():
                if c.status == "running":
                    self.installer.stop_by_name(c.name)

            # 步骤 3 & 4：启动后立即停止（确保容器可用 + copy-back 前置停止）
            logger.info("[%s] 步骤3-4: 启动并停止容器 %s", instance_name, container)
            self.installer.start(mysql_version)
            self.installer.stop(mysql_version)

            # 步骤 5：清空 datadir
            logger.info("[%s] 步骤5: 清空 datadir", instance_name)
            self.installer.clean_datadir(mysql_version)

            # 步骤 6：xtrabackup --prepare
            xb_prepare_log = f"{log_dir}/xb_prepare.log"
            logger.info("[%s] 步骤6: xtrabackup prepare", instance_name)
            self.xtrabackup.prepare(backup_remote_path, log_file=xb_prepare_log)
            result.logs["xb_prepare"] = xb_prepare_log

            # 步骤 7：xtrabackup --copy-back
            xb_copyback_log = f"{log_dir}/xb_copyback.log"
            datadir = self.installer.datadir(mysql_version)
            logger.info("[%s] 步骤7: xtrabackup copy-back -> %s", instance_name, datadir)
            self.xtrabackup.copy_back(backup_remote_path, datadir, log_file=xb_copyback_log)
            result.logs["xb_copyback"] = xb_copyback_log

            # 步骤 8：chown 999:999 datadir
            logger.info("[%s] 步骤8: chown datadir", instance_name)
            self.installer.chown_datadir(mysql_version)

            # 步骤 9：启动容器
            logger.info("[%s] 步骤9: 启动容器", instance_name)
            self.installer.start(mysql_version)

            # 步骤 10：验证（ADR-10 自动发现）
            logger.info("[%s] 步骤10: 验证", instance_name)
            verify_result = self.verifier.verify()
            result.verify_result = verify_result
            result.logs["verify"] = f"{log_dir}/verify.log"

            # 步骤 11：归档日志
            logger.info("[%s] 步骤11: 归档日志", instance_name)

            if not verify_result.passed:
                # 验证失败：采集 docker logs + error log（ADR-10）
                self._collect_error_logs(mysql_version, log_dir, result)
                result.success = False
                result.error_msg = verify_result.detail
            else:
                result.success = True

        except RecoveryError as e:
            logger.error("[%s] 恢复失败: %s", instance_name, e)
            result.success = False
            result.error_msg = str(e)
            self._collect_error_logs(mysql_version, log_dir, result)
        except Exception as e:
            logger.error("[%s] 恢复异常: %s", instance_name, e, exc_info=True)
            result.success = False
            result.error_msg = f"未预期异常: {e}"
            self._collect_error_logs(mysql_version, log_dir, result)

        result.duration_sec = int((datetime.now(timezone.utc) - start).total_seconds())
        return result

    def _collect_error_logs(self, version: str, log_dir: str, result: TaskResult) -> None:
        """采集错误日志（ADR-10：启动失败时保留 docker logs + error log）。"""
        try:
            # docker logs
            docker_logs = self.installer.get_docker_logs(version)
            docker_log_path = f"{log_dir}/error/docker.log"
            self.executor.run(f"mkdir -p {log_dir}/error")
            self.executor.run(f"cat > {docker_log_path} << 'LOGEOF'\n{docker_logs}\nLOGEOF")
            result.logs["docker_error"] = docker_log_path

            # MySQL error log（datadir 下）
            datadir = self.installer.datadir(version)
            err_check = self.executor.run(f"ls {datadir}/*.err 2>/dev/null")
            if err_check.stdout.strip():
                err_file = err_check.stdout.strip().splitlines()[0]
                err_dest = f"{log_dir}/error/mysql.err"
                self.executor.run(f"cp {err_file} {err_dest}")
                result.logs["mysql_error"] = err_dest
        except Exception as e:
            logger.warning("采集错误日志失败: %s", e)

    def _dry_run(
        self, version: str, backup_path: str, instance: str, container: str
    ) -> TaskResult:
        """dry-run 模式：只打印计划不执行。"""
        logger.info("[dry-run] %s: 容器=%s 版本=%s 备份=%s", instance, container, version, backup_path)
        return TaskResult(
            success=True,
            error_msg="[dry-run] 未实际执行",
            container_name=container,
        )
