"""Verifier 单元测试（mock pymysql，测试 ADR-10 自动发现逻辑）。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from toolkit.recovery.verifier import VerifyResult, Verifier, DEFAULT_SYSTEM_DBS


def _mock_cursor(fetchall_returns=None, fetchone_returns=None):
    """造一个 mock cursor。"""
    cur = MagicMock()
    # 支持 fetchall/fetchone 的多次调用返回不同值
    if fetchall_returns is not None:
        cur.fetchall.side_effect = (
            fetchall_returns if isinstance(fetchall_returns, list) else [fetchall_returns]
        )
    if fetchone_returns is not None:
        cur.fetchone.side_effect = (
            fetchone_returns if isinstance(fetchone_returns, list) else [fetchone_returns]
        )
    return cur


class TestVerifyResult:
    def test_passed_true(self):
        r = VerifyResult(passed=True, detail="ok")
        assert r.passed is True

    def test_default_fields(self):
        r = VerifyResult(passed=False, detail="fail")
        assert r.verify_count == -1
        assert r.verify_db == ""


class TestVerifierNoBusinessDb:
    """边界：无业务库 → PASS。"""

    def test_only_system_dbs_passes(self):
        verifier = Verifier()
        mock_conn = MagicMock()
        mock_cur = _mock_cursor(
            fetchall_returns=[[("mysql",), ("information_schema",), ("sys",)]],
            fetchone_returns=None,
        )
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        with patch.object(verifier, "_get_connection", return_value=mock_conn):
            result = verifier.verify()

        assert result.passed is True
        assert "无业务库" in result.detail


class TestVerifierWithBusinessDb:
    """正常路径：有业务库 → 选表最多 → 有数据表 → COUNT>0。"""

    def test_count_positive_passes(self):
        verifier = Verifier()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        # fetchall 被调用两次：1.所有库列表  2.表数量统计
        mock_cur.fetchall.side_effect = [
            [("mysql",), ("information_schema",), ("orders",)],  # 所有库
            [("orders", 5)],  # 表数量统计（orders 表最多）
        ]
        # fetchone 被调用两次：1.选有数据的表  2.COUNT 结果
        mock_cur.fetchone.side_effect = [
            ("order_info",),  # 选有数据的表
            (42,),            # COUNT 结果
        ]
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        with patch.object(verifier, "_get_connection", return_value=mock_conn):
            result = verifier.verify()

        assert result.passed is True
        assert result.verify_db == "orders"
        assert result.verify_table == "order_info"
        assert result.verify_count == 42

    def test_count_zero_fails(self):
        verifier = Verifier()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        # fetchall 两次：1.所有库  2.表数量统计
        mock_cur.fetchall.side_effect = [
            [("mysql",), ("orders",)],  # 所有库
            [("orders", 3)],  # 表数量统计
        ]
        # fetchone 三次：1.无有数据的表(None) 2.取第一张表 3.COUNT=0
        mock_cur.fetchone.side_effect = [
            None,              # 无 TABLE_ROWS > 0 的表
            ("order_info",),   # 取第一张表
            (0,),              # COUNT = 0
        ]
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        with patch.object(verifier, "_get_connection", return_value=mock_conn):
            result = verifier.verify()

        assert result.passed is False
        assert result.verify_count == 0


class TestVerifierConnectionFailure:
    """连接失败 → FAIL。"""

    def test_connection_error_returns_fail(self):
        verifier = Verifier()
        from toolkit.core.exceptions import VerifyError

        with patch.object(
            verifier,
            "_get_connection",
            side_effect=VerifyError("连接失败"),
        ):
            result = verifier.verify()

        assert result.passed is False
        assert "连接失败" in result.detail


class TestVerifierSystemDbFilter:
    """系统库过滤名单正确。"""

    def test_default_system_dbs(self):
        assert "mysql" in DEFAULT_SYSTEM_DBS
        assert "information_schema" in DEFAULT_SYSTEM_DBS
        assert "performance_schema" in DEFAULT_SYSTEM_DBS
        assert "sys" in DEFAULT_SYSTEM_DBS
        assert "test" in DEFAULT_SYSTEM_DBS
