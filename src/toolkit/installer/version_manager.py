"""MySQL 版本缓存与查找。

管理已安装版本、版本排序、版本与 xtrabackup 的匹配矩阵。
Sprint 1 实现。
"""
from __future__ import annotations


class VersionManager:
    """版本管理器。"""

    def __init__(self, install_root: str, version_matrix: dict[str, str] | None = None):
        self.install_root = install_root
        # xtrabackup 版本矩阵：{"5.7": "percona-xtrabackup-2.4", "8.0": "percona-xtrabackup-8.0"}
        self.version_matrix = version_matrix or {}

    def list_installed(self) -> list[str]:
        """列出已登记的 MySQL 版本（从元数据库查常驻容器池）。

        注意：ADR-09 后，"已安装"等价于"常驻容器已创建"。
        本方法需要传入 session 或在 installer 层查询，这里保留接口兼容。
        """
        # Sprint 1: 实际查询在 DockerInstaller 里做（查 mysql_containers 表）
        raise NotImplementedError("请在 DockerInstaller 中通过 DB 查询")

    def is_installed(self, version: str) -> bool:
        """某版本是否已登记（Sprint 1 在 installer 层实现）。"""
        raise NotImplementedError("请在 DockerInstaller 中通过 DB 查询")

    @staticmethod
    def container_name_for(version: str, prefix: str = "drill-mysql") -> str:
        """版本号 → 容器名（ADR-09：drill-mysql-{version去点}）。"""
        return f"{prefix}-{version.replace('.', '')}"

    @staticmethod
    def datadir_for(version: str, base: str = "/data/drill") -> str:
        """版本号 → datadir 路径（ADR-09）。"""
        return f"{base}/{version}/datadir"

    def xtrabackup_for(self, version: str) -> str:
        """返回该 MySQL 版本对应的 xtrabackup 版本。"""
        major_minor = ".".join(version.split(".")[:2])
        if major_minor not in self.version_matrix:
            raise ValueError(f"未配置 {major_minor} 的 xtrabackup 版本矩阵")
        return self.version_matrix[major_minor]

    @staticmethod
    def sort_versions_asc(versions: list[str]) -> list[str]:
        """版本号升序排序（如 5.7.44 < 8.0.35）。

        用于 PRD FP-03：恢复任务按版本升序排队，减少重装。
        """
        def key(v: str) -> tuple[int, ...]:
            return tuple(int(x) for x in v.split("."))
        return sorted(versions, key=key)
