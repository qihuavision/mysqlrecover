"""version_compat.py 单元测试（Sprint 6：redo 兼容组判断）。"""
from __future__ import annotations

import pytest

from toolkit.core.version_compat import (
    can_share_container,
    check_restore_safe,
    container_for_versions,
    redo_era,
    version_tuple,
)


class TestRedoEra:
    def test_57_series_same_era(self):
        """5.7 全系同一 redo 代。"""
        assert redo_era("5.7.30") == redo_era("5.7.44") == "5.7"

    def test_80_legacy(self):
        """8.0.0-8.0.29 旧 redo 代。"""
        assert redo_era("8.0.20") == "8.0-legacy"
        assert redo_era("8.0.29") == "8.0-legacy"

    def test_80_new_era(self):
        """8.0.30+ 新 redo 代（#innodb_redo 分水岭）。"""
        assert redo_era("8.0.30") == "8.0.30+"
        assert redo_era("8.0.35") == "8.0.30+"
        assert redo_era("8.0.36") == "8.0.30+"

    def test_legacy_and_new_differ(self):
        """8.0.29 与 8.0.30 不同代（redo 分水岭）。"""
        assert redo_era("8.0.29") != redo_era("8.0.30")

    def test_57_and_80_differ(self):
        assert redo_era("5.7.44") != redo_era("8.0.35")


class TestCanShareContainer:
    def test_same_era_upgrade_ok(self):
        """同代 + 容器 >= 备份 → 可共容器（升级安全）。"""
        assert can_share_container("8.0.20", "8.0.25") is True
        assert can_share_container("8.0.31", "8.0.36") is True
        assert can_share_container("5.7.30", "5.7.44") is True

    def test_downgrade_rejected(self):
        """容器低于备份版本 → 拒绝（MySQL 不支持降级）。"""
        assert can_share_container("8.0.35", "8.0.20") is False
        assert can_share_container("5.7.44", "5.7.30") is False

    def test_cross_redo_era_rejected(self):
        """跨 8.0.30 分水岭 → 拒绝。"""
        assert can_share_container("8.0.25", "8.0.35") is False
        assert can_share_container("8.0.35", "8.0.25") is False

    def test_cross_major_rejected(self):
        """跨大版本 → 拒绝。"""
        assert can_share_container("5.7.44", "8.0.35") is False
        assert can_share_container("8.0.35", "5.7.44") is False


class TestContainerForVersions:
    def test_picks_highest(self):
        """组内容器 = 最高版本。"""
        assert container_for_versions(["8.0.20", "8.0.35", "8.0.25"]) == "8.0.35"
        assert container_for_versions(["5.7.30", "5.7.44"]) == "5.7.44"

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            container_for_versions([])


class TestCheckRestoreSafe:
    def test_safe_restore(self):
        ok, _ = check_restore_safe("8.0.35", "8.0.35")
        assert ok is True
        ok, _ = check_restore_safe("8.0.31", "8.0.36")
        assert ok is True

    def test_major_mismatch(self):
        ok, reason = check_restore_safe("5.7.44", "8.0.35")
        assert ok is False
        assert "大版本" in reason

    def test_redo_era_mismatch(self):
        ok, reason = check_restore_safe("8.0.25", "8.0.35")
        assert ok is False
        assert "redo" in reason

    def test_downgrade(self):
        ok, reason = check_restore_safe("8.0.36", "8.0.35")
        assert ok is False
        assert "降级" in reason
