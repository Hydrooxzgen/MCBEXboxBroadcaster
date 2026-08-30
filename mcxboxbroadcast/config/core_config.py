"""Config dataclasses, ported from Java CoreConfig.java.

Field names and defaults mirror the Java interface; the YAML keys use the
hyphenated kebab-case forms produced by Configurate.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import constants


@dataclass
class SessionInfoConfig:
    host_name: str = "Geyser Test Server"
    world_name: str = "GeyserMC Demo & Test Server"
    players: int = 0
    max_players: int = 20
    ip: str = "test.geysermc.org"
    port: int = 19132


@dataclass
class IcePortRangeConfig:
    min: int = 0
    max: int = 0


@dataclass
class SessionConfig:
    update_interval: int = 30
    query_server: bool = True
    web_query_fallback: bool = False
    config_fallback: bool = False
    session_info: SessionInfoConfig = field(default_factory=SessionInfoConfig)
    ice_port_range: IcePortRangeConfig = field(default_factory=IcePortRangeConfig)
    # Standalone-only settings are written but unused by the geyser extension
    remote_address: str = "auto"
    remote_port: str = "auto"


@dataclass
class ExpiryConfig:
    enabled: bool = True
    days: int = 15
    check: int = 1800


@dataclass
class FriendSyncConfig:
    update_interval: int = 60
    auto_follow: bool = True
    auto_unfollow: bool = True
    initial_invite: bool = True
    expiry: ExpiryConfig = field(default_factory=ExpiryConfig)


@dataclass
class NotificationConfig:
    enabled: bool = False
    webhook_url: str = ""
    session_expired_message: str = (
        "<!here> Xbox Session expired, sign in again to update it.\n\n"
        "Use the following link to sign in: %s\nEnter the code: %s"
    )
    friend_restriction_message: str = (
        "%s (%s) has restrictions in place that prevent them from being "
        "friends with our account."
    )


@dataclass
class CoreConfig:
    session: SessionConfig = field(default_factory=SessionConfig)
    friend_sync: FriendSyncConfig = field(default_factory=FriendSyncConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    debug_mode: bool = False
    suppress_session_update_message: bool = False
    config_version: int = constants.CONFIG_VERSION


SESSION_EXPIRED_MESSAGE_DEFAULT = None
