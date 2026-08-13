"""SQLAlchemy ORM 模型（Sprint 1 T4）。

对应 docs/03-数据库设计.md 的 6 张表。
遵守迁移纪律（见数据库设计文档）：MySQL 兼容类型、ISO8601 时间、TEXT 存 JSON。
一套模型兼容 SQLite/MySQL，切换只改连接串。
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import BigInteger, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now_iso() -> str:
    """当前时间 ISO8601 字符串（应用层生成，不用 DB 时间函数）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TaskStatus(str, Enum):
    """恢复任务状态机（FP-05，docs/02 第 6 节）。

    PENDING → RUNNING → SUCCESS
                       → FAILED → RETRYING → SUCCESS
                                            → FAILED_FINAL
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    FAILED_FINAL = "FAILED_FINAL"
    ARCHIVED = "ARCHIVED"


class Base(DeclarativeBase):
    """所有模型的基类。"""


class MysqlContainer(Base):
    """MySQL 常驻容器池（ADR-09）。每版本一个常驻容器。"""

    __tablename__ = "mysql_containers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mysql_version: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    container_name: Mapped[str] = mapped_column(String(64), nullable=False)
    docker_image: Mapped[str] = mapped_column(String(128), nullable=False)
    datadir_path: Mapped[str] = mapped_column(String(256), nullable=False)
    drill_port: Mapped[int] = mapped_column(Integer, nullable=False, default=13306)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="created")
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, default=_now_iso)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False, default=_now_iso)

    def __repr__(self) -> str:
        return f"<MysqlContainer {self.container_name} {self.mysql_version} {self.status}>"


class Instance(Base):
    """源生产实例清单（对应 config/instances.yaml）。"""

    __tablename__ = "instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    host: Mapped[str] = mapped_column(String(64), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=3306)
    mysql_version: Mapped[str] = mapped_column(String(20), nullable=False)
    backup_source_host: Mapped[str] = mapped_column(String(64), nullable=False)
    backup_source_path: Mapped[str] = mapped_column(String(256), nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, default=_now_iso)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False, default=_now_iso)

    backups: Mapped[list[Backup]] = relationship(back_populates="instance", cascade="all, delete-orphan")
    tasks: Mapped[list[RecoveryTask]] = relationship(back_populates="instance")

    def __repr__(self) -> str:
        return f"<Instance {self.name} {self.host}:{self.port} v{self.mysql_version}>"


class Backup(Base):
    """备份记录。"""

    __tablename__ = "backups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(Integer, ForeignKey("instances.id"), nullable=False, index=True)
    backup_path: Mapped[str] = mapped_column(String(512), nullable=False)
    backup_type: Mapped[str] = mapped_column(String(10), nullable=False, default="full")
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    xtrabackup_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="available")
    started_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    discovered_at: Mapped[str] = mapped_column(String(32), nullable=False, default=_now_iso)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, default=_now_iso)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False, default=_now_iso)

    instance: Mapped[Instance] = relationship(back_populates="backups")

    def __repr__(self) -> str:
        return f"<Backup #{self.id} instance={self.instance_id} {self.status}>"


class DrillRun(Base):
    """演练批次。一次 drill run 对应一个批次。"""

    __tablename__ = "drill_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_host: Mapped[str] = mapped_column(String(64), nullable=False)
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    started_at: Mapped[str] = mapped_column(String(32), nullable=False, default=_now_iso)
    finished_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, default=_now_iso)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False, default=_now_iso)

    tasks: Mapped[list[RecoveryTask]] = relationship(back_populates="run")

    def __repr__(self) -> str:
        return f"<DrillRun #{self.id} {self.status} {self.success_count}/{self.total_count}>"


class RecoveryTask(Base):
    """恢复任务。每个实例每次恢复一个任务。"""

    __tablename__ = "recovery_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, ForeignKey("drill_runs.id"), nullable=False, index=True)
    instance_id: Mapped[int] = mapped_column(Integer, ForeignKey("instances.id"), nullable=False, index=True)
    backup_id: Mapped[int] = mapped_column(Integer, nullable=False)
    container_name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    started_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    verify_db: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verify_table: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verify_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, default=_now_iso)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False, default=_now_iso)

    run: Mapped[DrillRun] = relationship(back_populates="tasks")
    instance: Mapped[Instance] = relationship(back_populates="tasks")
    logs: Mapped[list[RecoveryLog]] = relationship(back_populates="task", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<RecoveryTask #{self.id} {self.status} attempt={self.attempt}>"


class RecoveryLog(Base):
    """验证与错误日志归档索引（ADR-10）。"""

    __tablename__ = "recovery_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("recovery_tasks.id"), nullable=False, index=True)
    log_dir: Mapped[str] = mapped_column(String(512), nullable=False)
    xb_log_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    verify_log_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_log_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    docker_log_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, default=_now_iso)

    task: Mapped[RecoveryTask] = relationship(back_populates="logs")

    def __repr__(self) -> str:
        return f"<RecoveryLog task={self.task_id} dir={self.log_dir}>"
