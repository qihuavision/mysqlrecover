"""恢复后验证（FP-04, ADR-10 自动发现）。

不用为每个实例手写验证 SQL，工具自动发现业务库表验证：
1. 连接恢复出的实例
2. 查所有库 → 过滤系统库
3. 若无业务库 → PASS（MySQL 能起来即成功）
4. 选表最多的库 → 选有数据的表 → COUNT(*) > 0 则 PASS
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pymysql

from toolkit.core.exceptions import VerifyError
from toolkit.core.logger import get_logger

logger = get_logger(__name__)

# 默认系统库过滤名单
DEFAULT_SYSTEM_DBS = frozenset({
    "mysql", "information_schema", "performance_schema", "sys", "test"
})


@dataclass
class VerifyResult:
    """验证结果。"""

    passed: bool
    detail: str  # 人类可读的结论
    verify_db: str = ""  # 选中的验证库（诊断用）
    verify_table: str = ""  # 选中的验证表（诊断用）
    verify_count: int = -1  # COUNT 结果（-1 表示未查询）
    evidence: str = ""  # 详细证据（供审计）
    error_logs: dict = field(default_factory=dict)  # 错误日志路径（启动失败时）


class Verifier:
    """恢复后验证器（ADR-10 自动发现规则）。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 13306,
        password_env: str = "DRILL_MYSQL_PWD",
        exclude_system_dbs: frozenset = DEFAULT_SYSTEM_DBS,
        connect_timeout: int = 30,
        expected_version: str = "",  # 期望的 MySQL 版本（防连错实例铁闸，Sprint 6）
    ):
        self.host = host
        self.port = port
        self.password_env = password_env
        self.exclude_system_dbs = exclude_system_dbs
        self.connect_timeout = connect_timeout
        self.expected_version = expected_version

    def set_expected_version(self, version: str) -> None:
        """设置期望版本（编排器在执行时按容器版本注入）。"""
        self.expected_version = version

    def _get_connection(self) -> pymysql.Connection:
        """连接恢复出的 MySQL（密码从环境变量读，ADR-10）。

        恢复出的 MySQL 是生产的字节级副本，用 --skip-grant-tables 启动后可免密登录。
        若设置了 DRILL_MYSQL_PWD 则用密码登录。
        """
        import os

        password = os.environ.get(self.password_env)
        try:
            return pymysql.connect(
                host=self.host,
                port=self.port,
                user="root",
                password=password or None,
                connect_timeout=self.connect_timeout,
                read_timeout=60,
                charset="utf8mb4",
            )
        except pymysql.Error as e:
            raise VerifyError(f"连接恢复实例 {self.host}:{self.port} 失败: {e}") from e

    def verify(self) -> VerifyResult:
        """执行 ADR-10 自动发现验证。

        Returns:
            VerifyResult
        """
        try:
            conn = self._get_connection()
        except VerifyError as e:
            return VerifyResult(
                passed=False,
                detail=f"连接失败: {e}",
                evidence=str(e),
            )

        try:
            return self._do_verify(conn)
        except Exception as e:
            logger.error("验证过程异常: %s", e, exc_info=True)
            return VerifyResult(
                passed=False,
                detail=f"验证过程异常: {e}",
                evidence=str(e),
            )
        finally:
            conn.close()

    def _do_verify(self, conn: pymysql.Connection) -> VerifyResult:
        """执行自动发现验证的核心逻辑。"""
        with conn.cursor() as cur:
            # 步骤 0：版本铁闸（Sprint 6）——确认连的是期望的恢复实例，
            # 防止端口被历史容器占用导致的假成功
            if self.expected_version:
                cur.execute("SELECT VERSION()")
                actual = str(cur.fetchone()[0])
                # "5.7.44-log" 之类带后缀的取前缀比较
                actual_clean = actual.split("-")[0]
                expected_clean = self.expected_version.split("-")[0]
                if not actual_clean.startswith(expected_clean):
                    return VerifyResult(
                        passed=False,
                        detail=(
                            f"连错实例（版本不匹配）：期望 {self.expected_version}，"
                            f"实际 {actual}（端口 {self.port} 可能被其他容器占用）"
                        ),
                        evidence=f"SELECT VERSION() = {actual} @ {self.host}:{self.port}",
                    )
                logger.info("版本铁闸通过: %s（期望 %s）", actual, self.expected_version)

            # 步骤 1：查所有库
            cur.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA")
            all_dbs = {row[0] for row in cur.fetchall()}
            business_dbs = all_dbs - self.exclude_system_dbs
            logger.info("所有库: %s, 过滤系统库后业务库: %s", all_dbs, business_dbs)

            # 步骤 2：若无业务库 → PASS（MySQL 能起来即成功）
            if not business_dbs:
                return VerifyResult(
                    passed=True,
                    detail="无业务库，但 MySQL 已成功启动，判定 PASS",
                    evidence=f"所有库: {sorted(all_dbs)}",
                )

            # 步骤 3：选表最多的库
            placeholders = ",".join(["%s"] * len(business_dbs))
            cur.execute(
                f"SELECT TABLE_SCHEMA, COUNT(*) as cnt "
                f"FROM information_schema.TABLES "
                f"WHERE TABLE_SCHEMA IN ({placeholders}) "
                f"GROUP BY TABLE_SCHEMA ORDER BY cnt DESC",
                tuple(business_dbs),
            )
            db_counts = cur.fetchall()
            if not db_counts:
                return VerifyResult(
                    passed=True,
                    detail="业务库存在但无表，MySQL 已成功启动，判定 PASS",
                    verify_db="",
                )
            selected_db = db_counts[0][0]
            logger.info("选中验证库: %s（表数最多）", selected_db)

            # 步骤 4：选有数据的第一张表（TABLE_ROWS > 0）
            cur.execute(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_ROWS > 0 "
                "ORDER BY TABLE_NAME LIMIT 1",
                (selected_db,),
            )
            row = cur.fetchone()
            if row is None:
                # 库里所有表都没数据 → 选第一张表 COUNT（会得到 0 → FAIL）
                cur.execute(
                    "SELECT TABLE_NAME FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME LIMIT 1",
                    (selected_db,),
                )
                row = cur.fetchone()
                if row is None:
                    return VerifyResult(
                        passed=True,
                        detail=f"库 {selected_db} 无表，MySQL 已成功启动，判定 PASS",
                        verify_db=selected_db,
                    )
            selected_table = row[0]
            logger.info("选中验证表: %s", selected_table)

            # 步骤 5：COUNT(*)
            # 表名/库名用反引号包裹防注入（来自 information_schema，可信但保险）
            cur.execute(f"SELECT COUNT(*) FROM `{selected_db}`.`{selected_table}`")
            count = cur.fetchone()[0]
            logger.info("COUNT(%s.%s) = %s", selected_db, selected_table, count)

            # 判定：COUNT > 0 则 PASS
            if count > 0:
                return VerifyResult(
                    passed=True,
                    detail=f"验证通过：{selected_db}.{selected_table} COUNT={count}",
                    verify_db=selected_db,
                    verify_table=selected_table,
                    verify_count=count,
                    evidence=f"SELECT COUNT(*) FROM `{selected_db}`.`{selected_table}` = {count}",
                )
            else:
                return VerifyResult(
                    passed=False,
                    detail=f"验证失败：{selected_db}.{selected_table} COUNT=0（无数据）",
                    verify_db=selected_db,
                    verify_table=selected_table,
                    verify_count=0,
                    evidence=f"SELECT COUNT(*) FROM `{selected_db}`.`{selected_table}` = 0",
                )
        # cursor 上下文自动关闭
