"""Notification manager, mirroring the Java notification interfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod

from mcxboxbroadcast.config.core_config import NotificationConfig
from mcxboxbroadcast.logger import Logger


class NotificationManager(ABC):
    @abstractmethod
    def send_session_expired_notification(self, verification_uri: str, user_code: str) -> None: ...

    @abstractmethod
    def send_friend_restriction_notification(self, username: str, xuid: str) -> None: ...


class BaseNotificationManager(NotificationManager):
    def __init__(self, logger: Logger, config: NotificationConfig) -> None:
        self.logger = logger
        self.config = config

    def send_session_expired_notification(self, verification_uri: str, user_code: str) -> None:
        message = self.config.session_expired_message % (verification_uri, user_code)
        self.send_notification(message)

    def send_friend_restriction_notification(self, username: str, xuid: str) -> None:
        message = self.config.friend_restriction_message % (username, xuid)
        self.send_notification(message)

    def send_notification(self, message: str) -> None:
        # Overridden by concrete implementations
        raise NotImplementedError
