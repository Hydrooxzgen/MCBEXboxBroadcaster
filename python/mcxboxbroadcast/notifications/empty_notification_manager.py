"""No-op notification manager used when notifications are disabled."""

from __future__ import annotations

from .notification_manager import BaseNotificationManager


class EmptyNotificationManager(BaseNotificationManager):
    def send_notification(self, message: str) -> None:
        # Intentionally does nothing
        pass
