"""Notification manager interface."""

from __future__ import annotations

import abc


class NotificationManager(abc.ABC):
    @abc.abstractmethod
    def send_session_expired_notification(self, verification_uri: str, user_code: str) -> None: ...

    @abc.abstractmethod
    def send_friend_restriction_notification(self, username: str, xuid: str) -> None: ...
