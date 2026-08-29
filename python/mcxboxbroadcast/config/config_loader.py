"""Load and merge ``config.yml`` into a :class:`CoreConfig`.

The on-disk YAML uses the same camelCase keys as the original Java project so
existing config files keep working. They are converted to snake_case to match
the Python dataclasses.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, fields, is_dataclass
from typing import Any

import yaml

from .core_config import (
    CoreConfig,
    SessionConfig,
    SessionInfoConfig,
    IcePortRangeConfig,
    FriendSyncConfig,
    ExpiryConfig,
    NotificationConfig,
)

_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")


def _camel_to_snake(name: str) -> str:
    return _CAMEL_RE.sub("_", name).lower()


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` into a copy of ``base``."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _dataclass_from_dict(cls: type, data: dict) -> Any:
    """Build a dataclass instance from a (snake_case) dict, recursing into nested dataclasses."""
    if not is_dataclass(cls):
        return data
    kwargs: dict[str, Any] = {}
    field_names = {f.name for f in fields(cls)}
    for key, value in data.items():
        snake = _camel_to_snake(key)
        if snake not in field_names:
            continue
        field_type = next(f.type for f in fields(cls) if f.name == snake)
        # Resolve string annotations lazily for nested dataclasses
        if isinstance(field_type, str):
            field_type = globals().get(field_type, None)
        if is_dataclass(field_type) and isinstance(value, dict):
            kwargs[snake] = _dataclass_from_dict(field_type, value)
        else:
            kwargs[snake] = value
    return cls(**kwargs)


def _default_dict() -> dict:
    """Return the default configuration as a plain dict (snake_case keys)."""
    return {
        "session": {
            "remote_address": "auto",
            "remote_port": "auto",
            "update_interval": 30,
            "query_server": True,
            "web_query_fallback": False,
            "config_fallback": False,
            "session_info": {
                "host_name": "Geyser Test Server",
                "world_name": "GeyserMC Demo & Test Server",
                "players": 0,
                "max_players": 20,
                "ip": "test.geysermc.org",
                "port": 19132,
            },
            "ice_port_range": {"min": 0, "max": 0},
        },
        "friend_sync": {
            "update_interval": 60,
            "auto_follow": True,
            "auto_unfollow": True,
            "initial_invite": True,
            "expiry": {"enabled": True, "days": 15, "check": 1800},
        },
        "notifications": {
            "enabled": False,
            "webhook_url": "",
            "session_expired_message": (
                "<!here> Xbox Session expired, sign in again to update it.\n\n"
                "Use the following link to sign in: %s\n"
                "Enter the code: %s"
            ),
            "friend_restriction_message": (
                "%s (%s) has restrictions in place that prevent them from being friends with our account."
            ),
        },
        "debug_mode": False,
        "suppress_session_update_message": False,
    }


def load_config(path: str, platform: str = "Standalone") -> CoreConfig:
    """Load ``config.yml`` from ``path`` and merge it over the defaults.

    ``platform`` is accepted for API compatibility with the Java loader but is
    currently unused (only the standalone flavour is ported).
    """
    user_data: dict = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
            if loaded:
                user_data = loaded

    merged = _deep_merge(_default_dict(), user_data)
    return _dataclass_from_dict(CoreConfig, merged)


__all__ = ["load_config", "CoreConfig"]
