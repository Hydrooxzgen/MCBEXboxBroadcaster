"""Storage manager interface, mirroring the Java ``StorageManager``."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Mapping, Optional


class PlayerHistoryStorage(ABC):
    @abstractmethod
    def is_first_run(self) -> bool: ...

    @abstractmethod
    def last_seen(self, xuid: str, last_seen: Optional[datetime] = None) -> Optional[datetime]: ...

    @abstractmethod
    def clear(self, xuid: str) -> None: ...

    @abstractmethod
    def all(self) -> Mapping[str, datetime]: ...


class StorageManager(ABC):
    @abstractmethod
    def cache(self, data: Optional[str] = None) -> Optional[str]: ...

    @abstractmethod
    def sub_sessions(self, data: Optional[str] = None) -> Optional[str]: ...

    @abstractmethod
    def last_session_response(self, data: Optional[str] = None) -> Optional[str]: ...

    @abstractmethod
    def current_session_response(self, data: Optional[str] = None) -> Optional[str]: ...

    @abstractmethod
    def sub_session(self, session_id: str) -> "StorageManager": ...

    @abstractmethod
    def screenshot(self) -> str: ...

    @abstractmethod
    def cleanup(self) -> None: ...

    @abstractmethod
    def player_history(self) -> PlayerHistoryStorage: ...
