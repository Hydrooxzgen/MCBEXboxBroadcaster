"""Slack/Discord webhook notification manager."""

from __future__ import annotations

import json

import requests

from .notification_manager import BaseNotificationManager


class SlackNotificationManager(BaseNotificationManager):
    def send_notification(self, message: str) -> None:
        if not self.config.enabled:
            return
        if not self.config.webhook_url:
            return

        payload = json.dumps({"text": message.replace("\n", "\\n")})
        try:
            requests.post(
                self.config.webhook_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
        except requests.RequestException as e:
            self.logger.error(f"Failed to send a slack notification: {message}", exc_info=e)
