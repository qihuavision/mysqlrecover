"""config.py 单元测试。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from toolkit.core.config import (
    Config,
    InstanceConfig,
    load_instances,
)
from toolkit.core.exceptions import ConfigError


# ---------- Config.load 测试 ----------


class TestConfigLoad:
    def test_load_minimal_config(self, tmp_path):
        """最小配置：只有 database。"""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("database:\n  url: sqlite:///test.db\n")
        cfg = Config.load(cfg_file)
        assert cfg.database.url == "sqlite:///test.db"
        # 默认值
        assert cfg.docker.drill_port == 13306
        assert cfg.drill.max_retry == 1
        assert cfg.verify.min_count == 0

    def test_load_full_config(self, tmp_path):
        """完整配置。"""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            "database:\n  url: sqlite:///x.db\n"
            "docker:\n  drill_port: 13306\n  supported_versions: ['8.0.35']\n"
            "target:\n  host: 10.0.0.100\n  ssh_user: root\n"
            "backup:\n  source_host: 10.0.0.200\n"
            "drill:\n  max_retry: 2\n"
        )
        cfg = Config.load(cfg_file)
        assert cfg.target.host == "10.0.0.100"
        assert cfg.backup.source_host == "10.0.0.200"
        assert cfg.drill.max_retry == 2
        assert cfg.docker.supported_versions == ["8.0.35"]

    def test_env_var_expansion(self, tmp_path, monkeypatch):
        """${ENV_VAR} 应被环境变量替换。"""
        monkeypatch.setenv("TEST_DB_PATH", "custom.db")
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("database:\n  url: 'sqlite:///${TEST_DB_PATH}'\n")
        cfg = Config.load(cfg_file)
        assert cfg.database.url == "sqlite:///custom.db"

    def test_env_var_unset_keeps_literal(self, tmp_path, monkeypatch):
        """未设置的环境变量保留原样。"""
        monkeypatch.delenv("UNSET_VAR_XYZ", raising=False)
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("database:\n  url: 'sqlite:///${UNSET_VAR_XYZ}'\n")
        cfg = Config.load(cfg_file)
        assert "${UNSET_VAR_XYZ}" in cfg.database.url

    def test_file_not_exist_raises(self, tmp_path):
        with pytest.raises(ConfigError, match="配置文件不存在"):
            Config.load(tmp_path / "nope.yaml")

    def test_invalid_config_raises(self, tmp_path):
        """target.host 必填，缺失应校验失败。"""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("target:\n  ssh_user: root\n")  # 缺 host
        with pytest.raises(ConfigError, match="配置校验失败"):
            Config.load(cfg_file)

    def test_verify_defaults(self, tmp_path):
        """验证配置默认值（ADR-10 系统库过滤名单）。"""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("database:\n  url: sqlite:///x.db\n")
        cfg = Config.load(cfg_file)
        assert "mysql" in cfg.verify.exclude_system_dbs
        assert "information_schema" in cfg.verify.exclude_system_dbs


# ---------- load_instances 测试 ----------


class TestLoadInstances:
    def test_load_valid_instances(self, tmp_path):
        f = tmp_path / "instances.yaml"
        f.write_text(
            "instances:\n"
            "  - name: db1\n    host: 10.0.0.1\n    port: 3306\n"
            "    mysql_version: '8.0.35'\n    backup_source_host: 10.0.0.9\n"
            "    backup_source_path: /backups/db1\n"
            "  - name: db2\n    host: 10.0.0.2\n    mysql_version: '5.7.44'\n"
            "    backup_source_host: 10.0.0.9\n    backup_source_path: /backups/db2\n"
        )
        instances = load_instances(f)
        assert len(instances) == 2
        assert instances[0].name == "db1"
        assert instances[1].mysql_version == "5.7.44"
        assert instances[0].enabled is True  # 默认

    def test_empty_instances_raises(self, tmp_path):
        f = tmp_path / "instances.yaml"
        f.write_text("instances: []\n")
        with pytest.raises(ConfigError, match="实例清单为空"):
            load_instances(f)

    def test_missing_required_field(self, tmp_path):
        f = tmp_path / "instances.yaml"
        f.write_text(
            "instances:\n  - name: db1\n    host: 10.0.0.1\n"
            "    mysql_version: '8.0.35'\n"  # 缺 backup_source_host
        )
        with pytest.raises(ConfigError, match="校验失败"):
            load_instances(f)

    def test_invalid_version_format(self, tmp_path):
        f = tmp_path / "instances.yaml"
        f.write_text(
            "instances:\n  - name: db1\n    host: 10.0.0.1\n"
            "    mysql_version: '8.0'\n"  # 格式不对
            "    backup_source_host: 10.0.0.9\n    backup_source_path: /b\n"
        )
        with pytest.raises(ConfigError, match="校验失败"):
            load_instances(f)

    def test_file_not_exist(self, tmp_path):
        with pytest.raises(ConfigError, match="实例清单文件不存在"):
            load_instances(tmp_path / "nope.yaml")


# ---------- InstanceConfig 单元测试 ----------


class TestInstanceConfig:
    def test_defaults(self):
        inst = InstanceConfig(
            name="x", host="10.0.0.1", mysql_version="8.0.35",
            backup_source_host="10.0.0.9", backup_source_path="/b",
        )
        assert inst.port == 3306
        assert inst.enabled is True

    def test_name_stripped(self):
        inst = InstanceConfig(
            name="  x  ", host="10.0.0.1", mysql_version="8.0.35",
            backup_source_host="10.0.0.9", backup_source_path="/b",
        )
        assert inst.name == "x"
