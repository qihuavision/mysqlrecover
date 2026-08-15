"""Docker 方式管理 MySQL 常驻容器池（FP-01, ADR-09）。

取代原 tarball 安装方案（ADR-08 升级）。核心职责：
- ensure_container(version)：确保某版本的常驻容器存在（不存在则 pull + run）
- start/stop/status：控制常驻容器
- 所有命令经 CommandExecutor（SSH 到恢复机执行）

常驻容器设计（ADR-09）：
- 每版本一个容器：drill-mysql-{version去点}，如 drill-mysql-8035
- 固定端口 13306（串行复用，不冲突）
- datadir 挂载到宿主机 /data/drill/{version}/datadir
- 容器常驻，只 start/stop 不 rm
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from toolkit.core.db import get_session_ctx
from toolkit.core.exceptions import InstallError
from toolkit.core.executor import CommandExecutor, ExecResult
from toolkit.core.logger import get_logger
from toolkit.core.models import MysqlContainer
from toolkit.installer.version_manager import VersionManager

logger = get_logger(__name__)


@dataclass
class ContainerInfo:
    """容器状态信息。"""

    name: str
    version: str
    status: str  # created/running/stopped/error/not_found
    exists: bool


class DockerInstaller:
    """管理恢复机上的 MySQL 常驻容器池（ADR-09）。

    所有操作通过 executor（SSHExecutor）在恢复机上执行 docker 命令。
    """

    # 容器内 mysql 用户 uid（官方镜像，copy-back 后 chown 用）
    MYSQL_UID = "999"

    def __init__(
        self,
        executor: CommandExecutor,
        drill_port: int = 13306,
        datadir_base: str = "/data/drill",
        container_prefix: str = "drill-mysql",
        image_template: str = "mysql:{version}",
        supported_versions: list[str] | None = None,
    ):
        self.executor = executor
        self.drill_port = drill_port
        self.datadir_base = datadir_base
        self.container_prefix = container_prefix
        self.image_template = image_template
        self.supported_versions = supported_versions or []

    # ---------- 容器名/路径生成 ----------

    def container_name(self, version: str) -> str:
        return VersionManager.container_name_for(version, self.container_prefix)

    def datadir(self, version: str) -> str:
        return VersionManager.datadir_for(version, self.datadir_base)

    def image(self, version: str) -> str:
        return self.image_template.format(version=version)

    # ---------- 版本校验 ----------

    def _check_version(self, version: str) -> None:
        """版本校验（Sprint 7：白名单可选）。

        supported_versions 为空 = 不限制（任何公司开箱即用，
        遇到新版本 docker run 自动拉镜像）；
        配了白名单则严格校验（保守环境的运维管控）。
        """
        if not self.supported_versions:
            return
        if version not in self.supported_versions:
            raise InstallError(
                f"版本 {version} 不在支持列表: {self.supported_versions}"
                f"（config docker.supported_versions 为空则不限制）"
            )

    # ---------- 确保容器存在（核心方法，FP-01）----------

    def ensure_container(self, version: str, dry_run: bool = False, port: int | None = None) -> str:
        """确保某版本常驻容器存在。不存在则创建。返回容器名。

        幂等：已存在则直接返回，不重复创建。

        Args:
            version: MySQL 版本
            dry_run: 只打印
            port: 容器监听端口（Sprint 5 多版本并行：每版本独立端口；
                  不传则用默认 drill_port）
        """
        self._check_version(version)
        use_port = port or self.drill_port
        name = self.container_name(version)
        datadir = self.datadir(version)
        image = self.image(version)

        # 检查容器是否已存在
        info = self.inspect(name)
        if info.exists:
            # 端口一致性校验（Sprint 6）：容器端口是创建时固化的，
            # 若与本次期望端口不一致（如历史单组模式建的）→ 删了重建
            db_port = self._get_registered_port(version)
            if db_port and db_port != use_port:
                logger.info(
                    "容器 %s 登记端口 %d 与期望端口 %d 不一致，重建容器",
                    name, db_port, use_port,
                )
                self.executor.run(f"docker rm -f {name}")
            else:
                logger.info("容器 %s 已存在（%s），跳过创建", name, info.status)
                self._upsert_db_record(version, name, image, datadir, info.status, use_port)
                return name

        if dry_run:
            logger.info("[dry-run] 将创建容器 %s（镜像 %s，端口 %d）", name, image, use_port)
            return name

        logger.info("创建容器 %s（镜像 %s，端口 %d）", name, image, use_port)
        # 1. 创建 datadir 目录
        self.executor.run_checked(f"mkdir -p {datadir}")
        # 2. docker run（联调验证过的参数；按版本定制，Sprint 6 支持 5.7）
        #    --skip-log-bin: 避免 xtrabackup 拷 binlog 失败（5.7/8.0 通用）
        #    8.0 专属: --mysqlx=0（关 X 协议）
        #              --default-authentication-plugin=mysql_native_password（避免 caching_sha2）
        #    5.7 天生 native password，且不认识上面两个 8.0 参数 → 不加
        major_minor = ".".join(version.split(".")[:2])
        extra_args = "--mysqlx=0 --default-authentication-plugin=mysql_native_password" \
            if major_minor == "8.0" else ""
        cmd = (
            f"docker run -d --network host --name {name} "
            f"-v {datadir}:/var/lib/mysql "
            f"--restart=no "
            f"{image} --port={use_port} --skip-log-bin {extra_args}"
        )
        try:
            self.executor.run_checked(cmd)
        except RuntimeError as e:
            raise InstallError(f"创建容器 {name} 失败: {e}") from e

        # 3. 立即停止（常驻容器保持 stopped 状态，恢复时才 start）
        self.executor.run(f"docker stop {name}")

        # 4. 记录到元数据库
        self._upsert_db_record(version, name, image, datadir, "created", use_port)
        logger.info("容器 %s 创建完成（端口 %d，stopped 状态）", name, use_port)
        return name

    # ---------- 容器启停 ----------

    def start(self, version: str, wait_ready: bool = True, port: int | None = None) -> None:
        """启动某版本容器。

        Args:
            wait_ready: 是否等待 MySQL TCP 端口就绪（联调发现需 35-40 秒）。
                        恢复流程中 copy-back 前的 start 不需要等待（马上要 stop）。
            port: MySQL 监听端口（多版本并行时每版本不同，不传用默认）
        """
        name = self.container_name(version)
        use_port = port or self.drill_port
        logger.info("启动容器 %s（端口 %d）", name, use_port)
        res = self.executor.run(f"docker start {name}")
        if not res.ok:
            raise InstallError(f"启动容器 {name} 失败: {res.stderr}")
        self._update_status(version, "running")
        if wait_ready:
            self.wait_ready(version, port=use_port)

    def wait_ready(self, version: str, timeout: int = 60, port: int | None = None) -> bool:
        """等待 MySQL TCP 端口就绪（联调细节：ping 假就绪，TCP 监听更晚）。

        用 docker exec 在容器内跑 mysqladmin ping（TCP 方式）。
        """
        name = self.container_name(version)
        use_port = port or self.drill_port
        import time

        logger.info("等待 %s MySQL 就绪（端口 %d，最多 %d 秒）...", name, use_port, timeout)
        for i in range(timeout // 3):
            # 用 TCP 方式 ping（比 socket 更可靠反映真实就绪状态）
            res = self.executor.run(
                f"docker exec {name} mysqladmin ping -h127.0.0.1 -P{use_port} "
                f"--silent 2>/dev/null"
            )
            if res.ok:
                logger.info("MySQL %s 已就绪（等待了 %d 秒）", version, i * 3)
                return True
            time.sleep(3)
        logger.warning("MySQL %s 在 %d 秒内未就绪", version, timeout)
        return False

    def stop(self, version: str) -> None:
        """停止某版本容器。"""
        name = self.container_name(version)
        logger.info("停止容器 %s", name)
        res = self.executor.run(f"docker stop {name}")
        if not res.ok:
            # 容器可能已停止，stop 返回非 0 但不影响
            logger.warning("停止容器 %s 返回非 0（可能已停止）: %s", name, res.stderr)
        self._update_status(version, "stopped")

    def stop_by_name(self, name: str) -> None:
        """按容器名停止（用于切版本时停掉旧容器）。"""
        logger.info("停止容器 %s", name)
        self.executor.run(f"docker stop {name}")

    # ---------- 容器状态查询 ----------

    def inspect(self, name: str) -> ContainerInfo:
        """查询容器状态。"""
        res = self.executor.run(
            f"docker inspect --format '{{{{.State.Status}}}}' {name} 2>/dev/null"
        )
        if not res.ok or not res.stdout.strip():
            return ContainerInfo(name=name, version="", status="not_found", exists=False)
        # docker 的状态值：created/running/paused/restarting/removing/dead/exited
        raw_status = res.stdout.strip()
        # exited → stopped（业务语义）
        status_map = {"exited": "stopped", "running": "running", "created": "created"}
        status = status_map.get(raw_status, raw_status)
        return ContainerInfo(name=name, version="", status=status, exists=True)

    def is_running(self, version: str) -> bool:
        """某版本容器是否在运行。"""
        name = self.container_name(version)
        return self.inspect(name).status == "running"

    def list_containers(self) -> list[ContainerInfo]:
        """列出所有常驻容器（按命名前缀过滤）。"""
        res = self.executor.run(
            f"docker ps -a --filter 'name={self.container_prefix}' "
            f"--format '{{{{.Names}}}} {{{{.Status}}}}'"
        )
        containers = []
        if not res.ok:
            return containers
        for line in res.stdout.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split(maxsplit=1)
            name = parts[0]
            status_raw = parts[1] if len(parts) > 1 else "unknown"
            # 简单映射 docker status 文本
            status = "running" if "Up" in status_raw else "stopped"
            containers.append(ContainerInfo(name=name, version="", status=status, exists=True))
        return containers

    # ---------- datadir 操作 ----------

    def clean_datadir(self, version: str) -> None:
        """清空 datadir（copy-back 前必须空）。"""
        datadir = self.datadir(version)
        logger.info("清空 datadir %s", datadir)
        # rm -rf 内容但保留目录本身
        self.executor.run_checked(f"rm -rf {datadir}/* {datadir}/.* 2>/dev/null; mkdir -p {datadir}")

    def chown_datadir(self, version: str) -> None:
        """修正 datadir 属主为容器内 mysql 用户（uid 999）。"""
        datadir = self.datadir(version)
        logger.info("chown datadir %s -> %s:%s", datadir, self.MYSQL_UID, self.MYSQL_UID)
        self.executor.run_checked(f"chown -R {self.MYSQL_UID}:{self.MYSQL_UID} {datadir}")

    # ---------- 日志采集（ADR-10：启动失败时调用）----------

    def get_docker_logs(self, version: str) -> str:
        """获取容器日志（启动失败排查用）。"""
        name = self.container_name(version)
        res = self.executor.run(f"docker logs {name} 2>&1")
        return res.stdout if res.ok else f"[获取日志失败] {res.stderr}"

    # ---------- 元数据库操作 ----------

    def _upsert_db_record(
        self, version: str, name: str, image: str, datadir: str, status: str,
        port: int | None = None,
    ) -> None:
        """新增或更新 mysql_containers 记录。"""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        session = get_session_ctx()
        try:
            record = session.query(MysqlContainer).filter_by(mysql_version=version).first()
            if record:
                record.status = status
                record.updated_at = now
                if port:
                    record.drill_port = port
            else:
                record = MysqlContainer(
                    mysql_version=version,
                    container_name=name,
                    docker_image=image,
                    datadir_path=datadir,
                    drill_port=port or self.drill_port,
                    status=status,
                )
                session.add(record)
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error("更新容器记录失败: %s", e)
        finally:
            session.close()

    def _update_status(self, version: str, status: str, port: int | None = None) -> None:
        """仅更新状态字段。"""
        self._upsert_db_record(
            version, self.container_name(version), self.image(version),
            self.datadir(version), status, port,
        )

    def _get_registered_port(self, version: str) -> int | None:
        """查询表内登记的容器端口（无则 None）。"""
        session = get_session_ctx()
        try:
            rec = session.query(MysqlContainer).filter_by(mysql_version=version).first()
            return rec.drill_port if rec else None
        finally:
            session.close()
