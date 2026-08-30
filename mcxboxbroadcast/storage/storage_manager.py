"""Storage manager interface, ported from Java StorageManager.java."""

from __future__ import annotations

import abc
from datetime import datetime
from typing import Optional


class PlayerHistoryStorage(abc.ABC):
    @abc.abstractmethod
    def is_first_run(self) -> bool: ...

    @abc.abstractmethod
    def last_seen(self, xuid: str, last_seen: Optional[datetime] = None) -> Optional[datetime]:
        """With an argument: record. Without: query (returns None if unknown)."""

    @abc.abstractmethod
    def clear(self, xuid: str) -> None: ...

    @abc.abstractmethod
    def all(self) -> dict[str, datetime]: ...


class StorageManager(abc.ABC):
    @abc.abstractmethod
    def cache(self, data: Optional[str] = None) -> str: ...

    @abc.abstractmethod
    def sub_sessions(self, data: Optional[str] = None) -> str: ...

    @abc.abstractmethod
    def last_session_response(self, data: Optional[str] = None) -> str: ...

    @abc.abstractmethod
    def current_session_response(self, data: Optional[str] = None) -> str: ...

    @abc.abstractmethod
    def sub_session(self, id: str) -> "StorageManager": ...

    @abc.abstractmethod
    def screenshot(self) -> Optional[bytes]: ...

    @abc.abstractmethod
    def screenshot_last_modified(self) -> float: ...

    @abc.abstractmethod
    def cleanup(self) -> None: ...

    @abc.abstractmethod
    def player_history(self) -> PlayerHistoryStorage: ...
