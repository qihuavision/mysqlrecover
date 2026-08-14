"""配置加载与校验（Sprint 1 T2）。

从 config/config.yaml 与 config/instances.yaml 读取配置。
密码等敏感值通过环境变量注入，绝不从配置文件读明文（ADR-10）。

设计：
- 用 pydantic v2 定义配置模型并校验
- 支持 ${ENV_VAR} 环境变量插值
- 安全校验：production_ip_whitelist、版本白名单
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from toolkit.core.exceptions import ConfigError

# 环境变量插值正则：${VAR_NAME}
_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _expand_env(value: str) -> str:
    """把 ${ENV_VAR} 替换为环境变量值。变量不存在则原样保留。"""
    def _replacer(match: re.Match) -> str:
        var_name = match.group(1)
        return os.environ.get(var_name, match.group(0))
    return _ENV_PATTERN.sub(_replacer, value)


def _expand_env_recursive(obj):
    """递归地对 dict/list/str 做环境变量插值。"""
    if isinstance(obj, str):
        return _expand_env(obj)
    if isinstance(obj, dict):
        return {k: _expand_env_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env_recursive(item) for item in obj]
    return obj


# ---------- 配置模型（pydantic v2）----------


class DatabaseConfig(BaseModel):
    url: str = "sqlite:///data/toolkit.db"


class DockerConfig(BaseModel):
    container_prefix: str = "drill-mysql"
    drill_port: int = 13306
    datadir_base: str = "/data/drill"
    image_template: str = "mysql:{version}"
    supported_versions: list[str] = Field(default_factory=list)


class XtrabackupConfig(BaseModel):
    binary_path: str = "/usr/bin/xtrabackup"
    version_matrix: dict[str, str] = Field(default_factory=dict)
    # 版本→二进制路径映射（Sprint 5：多版本 xtrabackup 共存）
    # 如 {"8.0": "/usr/bin/xtrabackup", "5.7": "/usr/bin/xtrabackup24"}
    binary_matrix: dict[str, str] = Field(default_factory=dict)


class TargetConfig(BaseModel):
    host: str
    ssh_user: str = "root"
    ssh_port: int = 22
    ssh_key_env: str = "DRILL_SSH_KEY"
    tmp_backup_dir: str = "/data/drill/tmp-backups"

    @property
    def ssh_key_path(self) -> str | None:
        """从环境变量读 SSH 私钥路径。"""
        return os.environ.get(self.ssh_key_env)


class BackupConfig(BaseModel):
    source_host: str
    source_ssh_user: str = "root"
    source_ssh_key_env: str = "DRILL_BACKUP_SSH_KEY"
    source_root: str = "/data/backups"
    naming_pattern: str = "{source_root}/{instance}/{date}"
    clean_tmp_after_drill: bool = True

    @property
    def source_ssh_key_path(self) -> str | None:
        return os.environ.get(self.source_ssh_key_env)


class DrillConfig(BaseModel):
    max_retry: int = 1
    serial: bool = True                    # 同版本组内串行（datadir 唯一，固定不变）
    parallel: bool = True                  # 跨版本并行（Sprint 5：每版本独立端口同时恢复）
    clean_datadir_before_copyback: bool = True
    dry_run_default: bool = True


class VerifyConfig(BaseModel):
    exclude_system_dbs: list[str] = Field(
        default_factory=lambda: ["mysql", "information_schema", "performance_schema", "sys", "test"]
    )
    select_db_strategy: str = "max_tables"
    select_table_strategy: str = "has_data"
    min_count: int = 0


class ArchiveConfig(BaseModel):
    root: str = "/data/archive"
    pattern: str = "{root}/{date}/{instance}_{version}"
    error_subdir: str = "error"


class ReportConfig(BaseModel):
    output_dir: str = "/data/logs/reports"


class WecomNotifierConfig(BaseModel):
    enabled: bool = True
    webhook_env: str = "DRILL_WECOM_WEBHOOK"
    notify_on: str = "always"  # always | on_failure

    @property
    def webhook_url(self) -> str | None:
        return os.environ.get(self.webhook_env)


class NotifiersConfig(BaseModel):
    wecom: WecomNotifierConfig = Field(default_factory=WecomNotifierConfig)


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "/data/logs/toolkit.log"
    rotate_size_mb: int = 100
    keep: int = 10


class SafetyConfig(BaseModel):
    production_ip_whitelist: list[str] = Field(default_factory=list)
    min_disk_free_gb: int = 50


class Config(BaseModel):
    """全局配置（对应 config.yaml）。"""

    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    docker: DockerConfig = Field(default_factory=DockerConfig)
    xtrabackup: XtrabackupConfig = Field(default_factory=XtrabackupConfig)
    target: TargetConfig | None = None
    backup: BackupConfig | None = None
    drill: DrillConfig = Field(default_factory=DrillConfig)
    verify: VerifyConfig = Field(default_factory=VerifyConfig)
    archive: ArchiveConfig = Field(default_factory=ArchiveConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    notifiers: NotifiersConfig = Field(default_factory=NotifiersConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        """从 YAML 加载配置，做环境变量插值 + pydantic 校验。"""
        path = Path(path)
        if not path.exists():
            raise ConfigError(f"配置文件不存在: {path}")
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        # 环境变量插值
        expanded = _expand_env_recursive(raw)
        try:
            return cls.model_validate(expanded)
        except Exception as e:
            raise ConfigError(f"配置校验失败: {e}") from e


# ---------- 实例清单模型 ----------


class InstanceConfig(BaseModel):
    """单个源实例配置（对应 instances.yaml 里的一项）。"""

    name: str
    host: str
    port: int = 3306
    mysql_version: str
    backup_source_host: str
    backup_source_path: str
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("实例名不能为空")
        return v.strip()

    @field_validator("mysql_version")
    @classmethod
    def version_format(cls, v: str) -> str:
        """版本号格式校验：x.y.z"""
        if not re.match(r"^\d+\.\d+\.\d+$", v):
            raise ValueError(f"版本号格式应为 x.y.z，得到: {v}")
        return v


def load_instances(path: str | Path) -> list[InstanceConfig]:
    """加载源实例清单。"""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"实例清单文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    instances_data = raw.get("instances", [])
    if not instances_data:
        raise ConfigError(f"实例清单为空: {path}")
    try:
        return [InstanceConfig.model_validate(item) for item in instances_data]
    except Exception as e:
        raise ConfigError(f"实例清单校验失败: {e}") from e
