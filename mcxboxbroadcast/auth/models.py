"""Auth token models and the lazy-refresh Holder, mirroring the holders in
MinecraftAuth's BedrockAuthManager."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Generic, Optional, TypeVar

T = TypeVar("T")


@dataclass
class MsaToken:
    expire_time_ms: int
    access_token: str
    refresh_token: Optional[str] = None


@dataclass
class XblDeviceToken:
    expire_time_ms: int
    token: str
    did: str


@dataclass
class XblUserToken:
    expire_time_ms: int
    token: str
    uhs: str


@dataclass
class XblTitleToken:
    expire_time_ms: int
    token: str


@dataclass
class XblXstsToken:
    expire_time_ms: int
    token: str
    user_hash: str

    @property
    def authorization_header(self) -> str:
        return f"XBL3.0 x={self.user_hash};{self.token}"


@dataclass
class PlayFabToken:
    expire_time_ms: int
    play_fab_id: str
    session_ticket: str


@dataclass
class MinecraftSession:
    expire_time_ms: int
    authorization_header: str


@dataclass
class MinecraftMultiplayerToken:
    expire_time_ms: int
    signed_token: str


@dataclass
class CachedProfileInfo:
    gamertag: str
    xuid: str
    expires_at: float = field(default_factory=lambda: time.time() + 600)


class Holder(Generic[T]):
    """Lazily refreshed value holder; refreshes when missing or expired."""

    def __init__(
        self,
        refresher: Callable[[], T],
        lock: Optional[threading.Lock] = None,
    ) -> None:
        self._refresher = refresher
        self._lock = lock or threading.Lock()
        self._cached: Optional[T] = None
        self._expire_time_ms: float = 0
        self.change_listeners: list[Callable[[T], None]] = []

    @property
    def has_value(self) -> bool:
        return self._cached is not None

    def get_cached(self) -> T:
        if self._cached is None:
            return self.get_up_to_date()
        return self._cached

    def get_up_to_date(self) -> T:
        with self._lock:
            if self._cached is None or time.time() * 1000 >= self._expire_time_ms:
                value = self._refresher()
                self.set(value)
            return self._cached

    def set(self, value: T, expire_time_ms: float = float("inf")) -> None:
        self._cached = value
        self._expire_time_ms = expire_time_ms
        for listener in list(self.change_listeners):
            try:
                listener(value)
            except Exception:
                pass

    def clear(self) -> None:
        self._cached = None
        self._expire_time_ms = 0
