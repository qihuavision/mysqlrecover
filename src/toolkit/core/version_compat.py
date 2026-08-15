"""MySQL 版本兼容性判断（Sprint 7 修订：完整 redo 分水岭）。

依据 Percona 官方文档查证（2026-08）：

redo log 格式变更历史（决定物理备份能否互相恢复的硬边界）：
- 5.6 / 5.7 / 8.0：data dictionary 与 redo 全不兼容（大版本硬边界，
  PXB 2.4 ↔ 5.6/5.7，PXB 8.0 ↔ 8.0）
- **MySQL 8.0.20**：修改 redo 格式（减小 undo tablespace 修改的 redo 记录体积）
  → PXB ≤8.0.11 不兼容 8.0.20+
  来源: docs.percona.com/percona-xtrabackup/8.0/about-xtrabackup.html
- **MySQL 8.0.30**：redo 大重构（#innodb_redo 目录替代 ib_logfile*，动态容量）
  → 旧 PXB 全部不兼容
  来源: mydbops.com/blog/beware-of-your-backup-before-upgrading-mysql-8-0-30

→ redo 兼容代（同代可共用容器恢复，跨代不恢复）：
    "5.6"        : 5.6.x
    "5.7"        : 5.7.x
    "8.0.11-19"  : 8.0.11 ~ 8.0.19
    "8.0.20-29"  : 8.0.20 ~ 8.0.29
    "8.0.30+"    : 8.0.30 及以上
    "8.4"        : 8.4.x

组内容器 = 组内最高版本（升级安全）；容器版本必须 >= 备份版本（MySQL 不支持降级）。
"""
from __future__ import annotations

from toolkit.core.logger import get_logger

logger = get_logger(__name__)


def version_tuple(version: str) -> tuple[int, ...]:
    """'8.0.35' → (8, 0, 35)。非法格式返回 (0,)。"""
    try:
        return tuple(int(x) for x in version.split("."))
    except ValueError:
        return (0,)


def redo_era(version: str) -> str:
    """判断备份所属的 redo 兼容代。

    同代 = redo 格式一致 = 可共用一个 MySQL 容器串行恢复。
    """
    v = version_tuple(version)
    major, minor = v[0], v[1]
    patch = v[2] if len(v) >= 3 else 0

    if (major, minor) == (5, 6):
        return "5.6"
    if (major, minor) == (5, 7):
        return "5.7"
    if (major, minor) == (8, 0):
        # 8.0.20 和 8.0.30 两个 redo 分水岭（Percona 官方查证）
        if patch >= 30:
            return "8.0.30+"
        if patch >= 20:
            return "8.0.20-29"
        return "8.0.11-19"
    if (major, minor) == (8, 4):
        return "8.4"
    # 未知版本（未来 9.x 等）：按大版本独立成组，不与他人混用
    return f"{major}.{minor}" if major else version


def can_share_container(backup_version: str, container_version: str) -> bool:
    """备份能否恢复到指定版本的容器。

    规则：
    1. redo 代必须一致（跨代物理不兼容）
    2. 容器版本 >= 备份版本（MySQL 不支持降级）
    """
    if redo_era(backup_version) != redo_era(container_version):
        return False
    return version_tuple(container_version) >= version_tuple(backup_version)


def container_for_versions(backup_versions: list[str]) -> str:
    """为一组备份选择容器版本：组内最高版本（升级安全，绝不降级）。"""
    if not backup_versions:
        raise ValueError("版本列表为空")
    return max(backup_versions, key=version_tuple)


def check_restore_safe(backup_version: str, container_version: str) -> tuple[bool, str]:
    """恢复安全性检查（编排器调用，不安全则拦截并说明原因）。

    Returns:
        (ok, reason)
    """
    bt, ct = version_tuple(backup_version), version_tuple(container_version)
    if bt[:2] != ct[:2]:
        return False, (
            f"大版本不匹配：备份 {backup_version} 不能恢复到 {container_version} 容器"
            f"（5.6/5.7/8.0 物理格式互不兼容）"
        )
    if redo_era(backup_version) != redo_era(container_version):
        return False, (
            f"redo 代不匹配：备份 {backup_version}（{redo_era(backup_version)}）与容器 "
            f"{container_version}（{redo_era(container_version)}）跨 redo 分水岭"
            f"（8.0.20 / 8.0.30 是格式变更点），不能直接恢复"
        )
    if ct < bt:
        return False, f"不允许降级：容器 {container_version} 低于备份版本 {backup_version}"
    return True, "OK"
