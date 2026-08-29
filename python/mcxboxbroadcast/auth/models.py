"""Authentication data models."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CachedProfileInfo:
    gamertag: str
    xuid: str
    expires_at: float = 0.0

    @property
    def expired(self) -> bool:
        import time

        return time.time() >= self.expires_at


@dataclass
class MsaToken:
    access_token: str
    refresh_token: str
    expires_at: float


@dataclass
class XblToken:
    token: str
    user_hash: str
    expires_at: float


@dataclass
class XstsToken:
    token: str
    user_hash: str
    xuid: str
    expires_at: float


@dataclass
class MinecraftToken:
    access_token: str
    expires_at: float


@dataclass
class DeviceToken:
    """XBL device token (from device.auth.xboxlive.com)."""

    token: str
    expires_at: float


@dataclass
class PlayFabToken:
    """PlayFab login result (session ticket used for the MC session token)."""

    session_ticket: str
    entity_token: str
    playfab_id: str
    expires_at: float
