"""数据库连接管理（Sprint 1 T4）。

提供引擎创建、会话工厂、建表。
一套模型兼容 SQLite/MySQL，切换只改连接串（ADR-03 + docs/03 迁移指南）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from toolkit.core.models import Base

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def init_db(url: str, echo: bool = False) -> Engine:
    """初始化数据库引擎 + 建表。

    Args:
        url: 数据库连接串。
            SQLite: sqlite:///data/toolkit.db
            MySQL:  mysql+pymysql://user:pass@host:3306/toolkit?charset=utf8mb4
        echo: 是否打印 SQL（调试用）

    Returns:
        SQLAlchemy Engine
    """
    global _engine, _SessionLocal

    # SQLite 特殊处理：确保目录存在
    if url.startswith("sqlite:///"):
        db_path = url.replace("sqlite:///", "", 1)
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    _engine = create_engine(url, echo=echo, future=True)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)

    # 建表（幂等，已存在则跳过）
    Base.metadata.create_all(_engine)
    return _engine


def get_engine() -> Engine:
    """获取已初始化的引擎。未初始化则报错。"""
    if _engine is None:
        raise RuntimeError("数据库未初始化，请先调用 init_db()")
    return _engine


def get_session() -> Generator[Session, None, None]:
    """获取数据库会话（上下文管理器用法）。

    用法：
        with get_session() as session:
            session.add(obj)
    或在 CLI 中：
        for session in get_session():
            ...
    """
    if _SessionLocal is None:
        raise RuntimeError("数据库未初始化，请先调用 init_db()")
    session = _SessionLocal()
    try:
        yield session
    finally:
        session.close()


def get_session_ctx() -> Session:
    """获取数据库会话（手动管理）。

    用法：
        session = get_session_ctx()
        try:
            ...
            session.commit()
        finally:
            session.close()
    """
    if _SessionLocal is None:
        raise RuntimeError("数据库未初始化，请先调用 init_db()")
    return _SessionLocal()


def reset_db() -> None:
    """重置全局引擎（测试用）。"""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
