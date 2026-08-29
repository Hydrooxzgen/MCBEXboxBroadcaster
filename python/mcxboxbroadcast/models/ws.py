"""WebSocket message types, mirroring ``core/models/ws/MessageType``."""

from __future__ import annotations

from enum import Enum


class MessageType(Enum):
    Subscribe = 1
    Unsubscribe = 2
    Event = 3
    Resync = 4

    @classmethod
    def from_value(cls, value: int) -> "MessageType":
        for member in cls:
            if member.value == value:
                return member
        return None


__all__ = ["MessageType"]
