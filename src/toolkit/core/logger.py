"""日志配置（Sprint 1 T3）。

统一日志格式，输出到文件（RotatingFileHandler）+ 控制台（StreamHandler）。
参考 pyxtrabackup 的 logging 实践（ADR-08 架构标杆）。
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 统一日志格式
_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_CONFIGURED = False


def setup_logging(
    level: str = "INFO",
    log_file: str | None = None,
    rotate_size_mb: int = 100,
    keep: int = 10,
) -> logging.Logger:
    """配置并返回根 logger。

    幂等：多次调用不会重复添加 handler。

    Args:
        level: 日志级别（DEBUG/INFO/WARNING/ERROR）
        log_file: 日志文件路径。None 则只输出到控制台
        rotate_size_mb: 单文件最大 MB，超过轮转
        keep: 保留的旧日志文件数
    """
    global _CONFIGURED

    root = logging.getLogger()
    # 幂等：已配置过则只更新级别
    if _CONFIGURED:
        root.setLevel(getattr(logging, level.upper(), logging.INFO))
        return root

    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)

    # 控制台 handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    # 文件 handler（可选）
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=rotate_size_mb * 1024 * 1024,
            backupCount=keep,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # 降低第三方库的噪音
    logging.getLogger("paramiko").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    _CONFIGURED = True
    return root


def get_logger(name: str) -> logging.Logger:
    """获取命名 logger（模块内调用）。"""
    return logging.getLogger(name)
