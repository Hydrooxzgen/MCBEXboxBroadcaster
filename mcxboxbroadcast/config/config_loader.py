"""YAML config loader with defaults, comments and v1->v2 migration.

Ported from Java ConfigLoader.java.
"""

from __future__ import annotations

import dataclasses
import os

import yaml

from .. import constants
from ..logger import Logger
from .core_config import CoreConfig

logger = Logger("Config")

FIELD_COMMENT = {
    "session": "Core session settings",
    "friend-sync": "Friend/follower list sync settings",
    "notifications": "Notification settings (e.g., Slack/Discord webhook)",
    "debug-mode": "Enable debug logging",
    "suppress-session-update-message": 'Suppresses "Updated session!" log into debug',
    "config-version": "Do not change!",
    "update-interval": (
        "The amount of time in seconds to update session information\n"
        "Warning: This can be no lower than 20 due to Xbox rate limits"
    ),
    "query-server": "Should we query the bedrock server to sync the session information",
    "web-query-fallback": (
        "This uses checker.geysermc.org for querying if the native ping fails\n"
        "This can be useful in the case of docker networks or routing problems "
        "causing the native ping to fail"
    ),
    "config-fallback": "Fallback to config values if all other server query methods fail",
    "session-info": "The data to broadcast over xbox live, this is the default if querying is enabled",
    "ice-port-range": (
        "Restrict the local UDP port range for WebRTC (NetherNet) ICE candidates,\n"
        "so only a small range needs opening behind a firewall or in Docker with\n"
        "host networking. Each in-progress join uses one port (freed once the player\n"
        "is transferred), so this caps concurrent joins, not total players.\n"
        "Leave both at 0 for the OS ephemeral range (default)."
    ),
    "game-mode": "The game mode to broadcast (Survival, Creative, Adventure)",
    "auto-friend": "Should we automatically accept friend requests",
    "initial-invite": "Should we automatically send an invite when a friend is added",
    "expiry": "Friend expiry settings",
    "enabled": "Should we unfriend people that haven't joined the server in a while",
    "days": "The amount of time in days before a friend is considered expired",
    "check": "How often to check in seconds for expired friends",
    "webhook-url": (
        "The webhook url to send the message to\n"
        'If you are using discord add "/slack" to the end of the webhook url'
    ),
    "host-name": "The host name to broadcast",
    "world-name": "The world name to broadcast",
    "players": "The current number of players",
    "max-players": "The maximum number of players",
    "ip": "The IP address of the server",
    "port": "The port of the server",
    "min": "Lowest UDP port to use, or 0 for the OS default",
    "max": "Highest UDP port to use, or 0 for the OS default",
    "remote-address": (
        "The IP address to broadcast, you likely want to change this to\n"
        "your servers public IP"
    ),
    "remote-port": (
        "The port to broadcast, this should be left as auto unless your\n"
        "manipulating the port using network rules or reverse proxies"
    ),
    "session-expired-message": "The message to send when the session is expired and needs to be updated",
    "friend-restriction-message": (
        "The message to send when a friend has restrictions in place that "
        "prevent them from being friends with our account"
    ),
}


def _kebab(name: str) -> str:
    return name.replace("_", "-")


def _snake(name: str) -> str:
    return name.replace("-", "_")


def _migrate(data: dict) -> dict:
    """Apply the version transformations matching Java ConfigLoader.TRANSFORMER."""
    version = data.get("config-version", 1)

    if version < 2:
        session = data.setdefault("session", {})

        # Extension only settings moved into "session"
        for key in ("remote-address", "remote-port", "update-interval"):
            if key in data:
                session[key] = data.pop(key)

        # Standalone only renames
        if "suppress-session-update-info" in data:
            data["suppress-session-update-message"] = data.pop("suppress-session-update-info")
        if "debug-log" in data:
            data["debug-mode"] = data.pop("debug-log")

        # Shared renames
        if "slack-webhook" in data:
            data["notifications"] = data.pop("slack-webhook")

        friend_sync = data.setdefault("friend-sync", {})
        expiry = friend_sync.setdefault("expiry", {})
        for old, new in (
            ("should-expire", "enabled"),
            ("expire-days", "days"),
            ("expire-check", "check"),
        ):
            if old in friend_sync:
                expiry[new] = friend_sync.pop(old)

    if version < 5:
        friend_sync = data.setdefault("friend-sync", {})
        if "auto-follow" in friend_sync:
            friend_sync["auto-friend"] = friend_sync.pop("auto-follow")
        friend_sync.pop("auto-unfollow", None)

    data["config-version"] = constants.CONFIG_VERSION
    return data


def _apply_defaults(node: dict, defaults) -> None:
    """Fill missing keys in `node` from a dataclass instance `defaults`, recursively."""
    for f in dataclasses.fields(defaults):
        key = _kebab(f.name)
        default_value = getattr(defaults, f.name)
        if dataclasses.is_dataclass(default_value):
            child = node.setdefault(key, {})
            _apply_defaults(child, default_value)
        else:
            node.setdefault(key, default_value)


def _prune_unknown(node: dict, defaults, prefix: str = "") -> None:
    valid = {_kebab(f.name) for f in dataclasses.fields(defaults)}
    for key in list(node.keys()):
        if key not in valid:
            logger.debug(f"Removing unknown config key {prefix}{key}")
            del node[key]
            continue
        child_default = getattr(defaults, _snake(key))
        if dataclasses.is_dataclass(child_default) and isinstance(node[key], dict):
            _prune_unknown(node[key], child_default, prefix=f"{prefix}{key}.")


def _build_config(node: dict, cls):
    """Build a dataclass tree from a dict, ignoring invalid values."""
    defaults = cls()
    kwargs = {}
    for f in dataclasses.fields(cls):
        key = _kebab(f.name)
        value = node.get(key)
        default_value = getattr(defaults, f.name)
        if dataclasses.is_dataclass(default_value):
            if isinstance(value, dict):
                kwargs[f.name] = _build_config(value, type(default_value))
            else:
                kwargs[f.name] = default_value
        else:
            if value is None:
                kwargs[f.name] = default_value
            elif isinstance(default_value, bool):
                kwargs[f.name] = bool(value)
            elif isinstance(default_value, int) and not isinstance(default_value, bool):
                try:
                    kwargs[f.name] = int(value)
                except (TypeError, ValueError):
                    kwargs[f.name] = default_value
            elif isinstance(default_value, str):
                kwargs[f.name] = str(value)
            else:
                kwargs[f.name] = value
    return cls(**kwargs)


def _scalar_to_yaml(value, indent: int = 0) -> str:
    if isinstance(value, str) and "\n" in value:
        # Emit a literal block scalar for multi-line strings
        pad = "  " * (indent + 1)
        body = "\n".join(pad + line for line in value.splitlines())
        return "|-\n" + body
    text = yaml.safe_dump(
        value, default_flow_style=False, allow_unicode=True, width=4096
    )
    # Some PyYAML versions append the document end marker for plain scalars
    if text.endswith("...\n"):
        text = text[: -len("...\n")]
    return text.strip()


def _dump_yaml(config: CoreConfig) -> str:
    """Serialize the config to a commented YAML string with ordered keys."""
    lines: list[str] = []

    def emit(obj, indent: int) -> None:
        pad = "  " * indent
        for f in dataclasses.fields(obj):
            key = _kebab(f.name)
            value = getattr(obj, f.name)
            comment = FIELD_COMMENT.get(key)
            if comment:
                for cl in comment.splitlines():
                    lines.append(f"{pad}# {cl}")
            if dataclasses.is_dataclass(value):
                lines.append(f"{pad}{key}:")
                emit(value, indent + 1)
            else:
                lines.append(f"{pad}{key}: {_scalar_to_yaml(value, indent)}")

    emit(config, 0)
    return "\n".join(lines) + "\n"


def load_config(config_path: str) -> CoreConfig:
    """Load (or create) the config file, migrate and save if the version changed."""
    originally_empty = not os.path.exists(config_path) or os.path.getsize(config_path) == 0
    node: dict = {}
    if not originally_empty:
        with open(config_path, "r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh)
        if isinstance(loaded, dict):
            node = loaded

    version = node.get("config-version", 1)
    migrated = False
    if not originally_empty and node:
        if version < constants.CONFIG_VERSION:
            node = _migrate(node)
            migrated = True
            logger.info(
                f"Migrated config from version {version} to {constants.CONFIG_VERSION}"
            )
        _prune_unknown(node, CoreConfig())
    _apply_defaults(node, CoreConfig())
    node["config-version"] = constants.CONFIG_VERSION

    config = _build_config(node, CoreConfig)

    # Save when the file was created or migrated so the on-disk copy is current
    if originally_empty or migrated:
        save_config(config_path, config)

    return config


def save_config(config_path: str, config: CoreConfig) -> None:
    """Save the config with comments."""
    with open(config_path, "w", encoding="utf-8") as fh:
        fh.write(_dump_yaml(config))
