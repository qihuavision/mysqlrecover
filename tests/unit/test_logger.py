"""logger.py 单元测试。"""
from __future__ import annotations

import logging
import importlib

import pytest

from toolkit.core import logger as logger_mod
from toolkit.core.logger import setup_logging, get_logger


class TestSetupLogging:
    def test_returns_root_logger(self):
        root = setup_logging(level="DEBUG")
        assert isinstance(root, logging.Logger)
        assert root.level == logging.DEBUG

    def test_idempotent_no_duplicate_handlers(self):
        """多次调用不应重复添加 handler。"""
        setup_logging(level="INFO")
        count1 = len(logging.getLogger().handlers)
        setup_logging(level="WARNING")
        count2 = len(logging.getLogger().handlers)
        assert count1 == count2  # handler 数量不变

    def test_file_handler_created(self, tmp_path):
        """指定 log_file 时应创建文件 handler。"""
        # 重置模块状态以重新初始化
        importlib.reload(logger_mod)
        log_file = tmp_path / "test.log"
        logger_mod.setup_logging(level="INFO", log_file=str(log_file))
        # 写一条日志
        lg = logger_mod.get_logger("test")
        lg.info("hello")
        # 确保文件被创建
        for h in logging.getLogger().handlers:
            h.flush()
        assert log_file.exists()

    def test_get_logger_returns_named_logger(self):
        lg = get_logger("my_module")
        assert lg.name == "my_module"
