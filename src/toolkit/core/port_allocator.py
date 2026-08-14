"""端口分配器（Sprint 5：多版本并行地基）。

每个 MySQL 版本分配一个独立端口，不同版本的容器可同时运行：
    5.7.44 → 13306
    8.0.35 → 13307
    8.0.36 → 13308
    ...

规则：
- 按版本号升序排序后，依次分配 base_port + index
- 同一版本集合内分配结果稳定（幂等）
- 版本未在集合中时可动态追加分配
"""
from __future__ import annotations

from toolkit.core.logger import get_logger

logger = get_logger(__name__)


class PortAllocator:
    """按 MySQL 版本分配恢复容器端口。"""

    def __init__(self, base_port: int = 13306):
        self.base_port = base_port
        self._ports: dict[str, int] = {}  # {version: port}

    @staticmethod
    def _version_key(version: str) -> tuple[int, ...]:
        """版本号转元组（排序用）：'8.0.35' → (8, 0, 35)"""
        try:
            return tuple(int(x) for x in version.split("."))
        except ValueError:
            return (0,)

    def assign(self, versions: list[str]) -> dict[str, int]:
        """为一组版本分配端口（幂等：重复调用结果一致）。

        按版本升序依次分配 base_port + index。
        已分配过的版本保持原端口不变。
        """
        # 合并已分配的 + 新版本，统一排序后分配
        all_versions = set(self._ports.keys()) | set(versions)
        sorted_versions = sorted(all_versions, key=self._version_key)

        new_alloc: dict[str, int] = {}
        for idx, ver in enumerate(sorted_versions):
            new_alloc[ver] = self.base_port + idx

        self._ports = new_alloc
        logger.info("端口分配: %s", self._ports)
        return dict(self._ports)

    def get(self, version: str) -> int | None:
        """查询某版本端口。未分配返回 None。"""
        return self._ports.get(version)

    def get_or_assign(self, version: str) -> int:
        """查询端口，未分配则动态分配。"""
        if version not in self._ports:
            self.assign([version])
        return self._ports[version]

    @property
    def allocation(self) -> dict[str, int]:
        """当前全部分配（只读视图）。"""
        return dict(self._ports)
