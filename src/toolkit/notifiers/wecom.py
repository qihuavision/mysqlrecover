"""企业微信通知（FP-07, Sprint 3）。

webhook URL 从环境变量读，不写配置文件（安全，ADR-10）。
失败重试 3 次（间隔退避），仍失败记录日志，不阻塞主流程。
"""
from __future__ import annotations

import os
import time

import requests

from toolkit.core.logger import get_logger
from .base import Notifier

logger = get_logger(__name__)


class WeComNotifier(Notifier):
    """企业微信群机器人通知（markdown 消息）。"""

    def __init__(
        self,
        webhook_env: str = "DRILL_WECOM_WEBHOOK",
        max_retry: int = 3,
        notify_on: str = "always",  # always | on_failure
    ):
        self.webhook_env = webhook_env
        self.max_retry = max_retry
        self.notify_on = notify_on

    @property
    def webhook(self) -> str | None:
        return os.environ.get(self.webhook_env)

    def should_notify(self, failed_count: int) -> bool:
        """根据 notify_on 策略决定是否通知。"""
        if self.notify_on == "on_failure":
            return failed_count > 0
        return True  # always

    def send(self, title: str, content: str) -> bool:
        """发送 markdown 消息到企业微信。

        Args:
            title: 标题（企业微信 markdown 无独立标题字段，拼进 content）
            content: markdown 正文

        Returns:
            是否成功。webhook 未配置返回 False（记警告，不抛异常）。
        """
        webhook = self.webhook
        if not webhook:
            logger.warning("企业微信 webhook 未配置（环境变量 %s），跳过通知", self.webhook_env)
            return False

        # 企业微信 markdown 消息格式
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": f"{title}\n{content}",
                # 标题加粗显示效果由 markdown 内容自身控制
            }
        }

        for attempt in range(1, self.max_retry + 1):
            try:
                resp = requests.post(webhook, json=payload, timeout=10)
                data = resp.json()
                if data.get("errcode") == 0:
                    logger.info("企业微信通知发送成功（第 %d 次尝试）", attempt)
                    return True
                # errcode != 0：如频率限制（errcode 45009）
                logger.warning(
                    "企业微信返回错误（第 %d 次）: errcode=%s errmsg=%s",
                    attempt, data.get("errcode"), data.get("errmsg"),
                )
            except Exception as e:
                logger.warning("企业微信发送异常（第 %d 次）: %s", attempt, e)

            # 退避：1s, 2s, 4s...
            if attempt < self.max_retry:
                time.sleep(2 ** (attempt - 1))

        logger.error("企业微信通知发送失败（已重试 %d 次），放弃", self.max_retry)
        return False
