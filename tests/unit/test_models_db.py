"""models.py + db.py 单元测试。

用 SQLite 内存库测试，不依赖真实数据库。
"""
from __future__ import annotations

import pytest

from toolkit.core.db import (
    get_engine,
    get_session_ctx,
    init_db,
    reset_db,
)
from toolkit.core.models import (
    Backup,
    DrillRun,
    Instance,
    MysqlContainer,
    RecoveryLog,
    RecoveryTask,
)


@pytest.fixture
def db():
    """每个测试用独立的内存 SQLite。"""
    init_db("sqlite:///:memory:")
    yield
    reset_db()


class TestInitDb:
    def test_init_creates_all_tables(self, db):
        """建表后所有 6 张表应存在。"""
        engine = get_engine()
        with engine.connect() as conn:
            from sqlalchemy import inspect
            inspector = inspect(conn)
            tables = set(inspector.get_table_names())
        expected = {
            "mysql_containers", "instances", "backups",
            "drill_runs", "recovery_tasks", "recovery_logs",
        }
        assert expected.issubset(tables)

    def test_init_idempotent(self, db):
        """重复 init 不报错（create_all 幂等）。"""
        init_db("sqlite:///:memory:")

    def test_get_engine_before_init_raises(self):
        reset_db()
        with pytest.raises(RuntimeError, match="数据库未初始化"):
            get_engine()


class TestMysqlContainer:
    def test_create_and_query(self, db):
        session = get_session_ctx()
        try:
            c = MysqlContainer(
                mysql_version="8.0.35",
                container_name="drill-mysql-8035",
                docker_image="mysql:8.0.35",
                datadir_path="/data/drill/8.0.35/datadir",
                drill_port=13306,
            )
            session.add(c)
            session.commit()
            assert c.id is not None

            found = session.query(MysqlContainer).filter_by(mysql_version="8.0.35").one()
            assert found.container_name == "drill-mysql-8035"
            assert found.status == "created"  # 默认值
        finally:
            session.close()

    def test_version_unique(self, db):
        session = get_session_ctx()
        try:
            session.add(MysqlContainer(
                mysql_version="8.0.35", container_name="a",
                docker_image="mysql:8.0.35", datadir_path="/d",
            ))
            session.commit()
            session.add(MysqlContainer(
                mysql_version="8.0.35", container_name="b",  # 重复版本
                docker_image="mysql:8.0.35", datadir_path="/d2",
            ))
            with pytest.raises(Exception):  # IntegrityError
                session.commit()
        finally:
            session.close()


class TestInstanceAndBackup:
    def test_instance_with_backups_relationship(self, db):
        session = get_session_ctx()
        try:
            inst = Instance(
                name="db1", host="10.0.0.1", port=3306,
                mysql_version="8.0.35",
                backup_source_host="10.0.0.9",
                backup_source_path="/backups/db1",
            )
            session.add(inst)
            session.commit()

            b1 = Backup(instance_id=inst.id, backup_path="/backups/db1/20260813")
            b2 = Backup(instance_id=inst.id, backup_path="/backups/db1/20260812")
            session.add_all([b1, b2])
            session.commit()

            # 关系查询
            session.refresh(inst)
            assert len(inst.backups) == 2
        finally:
            session.close()

    def test_instance_name_unique(self, db):
        session = get_session_ctx()
        try:
            session.add(Instance(
                name="dup", host="10.0.0.1", mysql_version="8.0.35",
                backup_source_host="10.0.0.9", backup_source_path="/b",
            ))
            session.commit()
            session.add(Instance(
                name="dup", host="10.0.0.2", mysql_version="8.0.35",
                backup_source_host="10.0.0.9", backup_source_path="/b2",
            ))
            with pytest.raises(Exception):
                session.commit()
        finally:
            session.close()


class TestDrillRunAndTask:
    def test_full_workflow(self, db):
        """完整流程：实例 → 批次 → 任务 → 日志。"""
        session = get_session_ctx()
        try:
            # 实例
            inst = Instance(
                name="prod-01", host="10.0.0.1", mysql_version="8.0.35",
                backup_source_host="10.0.0.9", backup_source_path="/b",
            )
            session.add(inst)
            session.commit()

            # 备份
            backup = Backup(instance_id=inst.id, backup_path="/b/full")
            session.add(backup)
            session.commit()

            # 批次
            run = DrillRun(target_host="10.0.0.100", total_count=1)
            session.add(run)
            session.commit()

            # 任务
            task = RecoveryTask(
                run_id=run.id, instance_id=inst.id, backup_id=backup.id,
                container_name="drill-mysql-8035",
            )
            session.add(task)
            session.commit()

            # 日志
            log = RecoveryLog(
                task_id=task.id, log_dir="/archive/20260813/prod-01_8.0.35",
                xb_log_path="/archive/.../xb.log",
            )
            session.add(log)
            session.commit()

            # 验证关系链
            session.refresh(run)
            assert len(run.tasks) == 1
            session.refresh(task)
            assert task.instance.name == "prod-01"
            assert len(task.logs) == 1
            assert task.status == "PENDING"  # 默认
            assert task.attempt == 1  # 默认
        finally:
            session.close()


class TestTimestampDefaults:
    def test_created_at_auto_set(self, db):
        """created_at/updated_at 应自动填充 ISO8601。"""
        session = get_session_ctx()
        try:
            c = MysqlContainer(
                mysql_version="5.7.44", container_name="c",
                docker_image="mysql:5.7.44", datadir_path="/d",
            )
            session.add(c)
            session.commit()
            assert c.created_at  # 非空
            assert "T" in c.created_at  # ISO8601 格式
            assert c.updated_at == c.created_at
        finally:
            session.close()
