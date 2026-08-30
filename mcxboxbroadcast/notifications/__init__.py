"""Notification managers, ported from the Java core.notifications package."""

from __future__ import annotations

import json
from typing import Optional

import requests

from ..config.core_config import NotificationConfig
from ..logger import Logger
from .notification_manager import NotificationManager


class BaseNotificationManager(NotificationManager):
    def __init__(self, logger: Logger, config: Optional[NotificationConfig]) -> None:
        self.logger = logger
        self.config = config

    def send_session_expired_notification(self, verification_uri: str, user_code: str) -> None:
        if self.config is not None:
            self.send_notification(
                self.config.session_expired_message % (verification_uri, user_code)
            )

    def send_friend_restriction_notification(self, username: str, xuid: str) -> None:
        if self.config is not None:
            self.send_notification(self.config.friend_restriction_message % (username, xuid))

    def send_notification(self, message: str) -> None:
        if self.config is None or not self.config.enabled:
            return
        raise NotImplementedError


class EmptyNotificationManager(BaseNotificationManager):
    def __init__(self, logger: Logger) -> None:
        super().__init__(logger, None)

    def send_notification(self, message: str) -> None:
        pass


class SlackNotificationManager(BaseNotificationManager):
    def send_notification(self, message: str) -> None:
        if self.config is None or not self.config.enabled:
            return
        try:
            requests.post(
                self.config.webhook_url,
                json={"text": message.replace("\n", "\\n")},
                timeout=10,
            )
        except requests.RequestException as ex:
            self.logger.error(f"Failed to send a slack notification: {message}", ex)
