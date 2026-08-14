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

    def assign(self, versions: list[str], occupied_ports: dict[str, int] | None = None) -> dict[str, int]:
        """为一组版本（或 redo 代）分配端口。

        Args:
            versions: 本次需要的版本/代列表
            occupied_ports: 已存在的分配（如 {era/版本: port}，来自
                            mysql_containers 表的历史登记 + running 容器实际占用）。
                            已有的保持不变（全局一致，防历史容器端口冲突），
                            新的从未用的端口里按序分配。

        幂等：重复调用结果一致。
        """
        existing = dict(occupied_ports or {})
        used_ports = set(existing.values())

        # 保留历史分配（含未在本次 versions 里的）
        result: dict[str, int] = dict(existing)
        # 1. 已有分配保持不变
        for ver in versions:
            if ver in existing:
                result[ver] = existing[ver]

        # 2. 新的从未占用端口按序分配
        next_port = self.base_port
        for ver in sorted(set(versions) - set(result.keys()), key=self._version_key):
            while next_port in used_ports:
                next_port += 1
            result[ver] = next_port
            used_ports.add(next_port)
            next_port += 1

        self._ports = result
        logger.info("端口分配: %s（已有占用: %s）", self._ports, existing or "无")
        return dict(self._ports)

    def get(self, version: str) -> int | None:
        """查询某版本端口。未分配返回 None。"""
        return self._ports.get(version)

    def get_or_assign(self, version: str) -> int:
        """查询端口，未分配则动态分配（保留已分配的）。"""
        if version not in self._ports:
            self.assign([version], occupied_ports=self._ports)
        return self._ports[version]

    @property
    def allocation(self) -> dict[str, int]:
        """当前全部分配（只读视图）。"""
        return dict(self._ports)
