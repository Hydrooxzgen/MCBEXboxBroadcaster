"""Constants used across MCXboxBroadcast, mirroring the Java ``Constants`` class."""

from __future__ import annotations

import dataclasses
import enum
import json
from datetime import timedelta
from typing import Any
from urllib.parse import quote

# ---------------------------------------------------------------------------
# Bedrock protocol
# ---------------------------------------------------------------------------
# The Bedrock protocol version this tool emulates for the NetherNet redirect
# handshake. Update this when the client version you want to support changes.
BEDROCK_PROTOCOL_VERSION = 2169
BEDROCK_VERSION = "1.26.45"

# ---------------------------------------------------------------------------
# Xbox Live service identifiers
# ---------------------------------------------------------------------------
SERVICE_CONFIG_ID = "4fc10100-5f7a-4470-899b-280835760c07"  # Minecraft service config
TEMPLATE_NAME = "MinecraftLobby"
TITLE_ID = "896928775"  # Minecraft Windows Edition title id

CREATE_SESSION = (
    "https://sessiondirectory.xboxlive.com/serviceconfigs/"
    f"{SERVICE_CONFIG_ID}/sessionTemplates/{TEMPLATE_NAME}/sessions/%s"
)
JOIN_SESSION = "https://sessiondirectory.xboxlive.com/handles/%s/session"

RTA_WEBSOCKET = "wss://rta.xboxlive.com/connect"
CREATE_HANDLE = "https://sessiondirectory.xboxlive.com/handles"

PEOPLE = "https://social.xboxlive.com/users/me/people/xuid(%s)"
USER_PRESENCE = "https://userpresence.xboxlive.com/users/xuid(%s)/devices/current/titles/current"
FOLLOWERS = "https://peoplehub.xboxlive.com/users/me/people/followers"
SOCIAL = "https://peoplehub.xboxlive.com/users/me/people/social"
SOCIAL_SUMMARY = "https://social.xboxlive.com/users/me/summary"
FOLLOWER = "https://social.xboxlive.com/users/me/people/follower/xuid(%s)"

GALLERY = "https://persona.franchise.minecraft-services.net/api/v1.0/gallery"

WEBSOCKET_CONNECTION_TIMEOUT = timedelta(seconds=10)

# Gathered from scraped web requests, seems to use the below enum
# https://github.com/LiteLDev/LeviLamina/blob/main/src/mc/network/ConnectionType.h
CONNECTION_TYPE_JSON_RPC = 7

# Used to be 1000, but the limit was increased in Aug 2024
MAX_FRIENDS = 2000

# Config version for upgrade purposes
CONFIG_VERSION = 2


def _to_serializable(obj: Any) -> Any:
    """Convert dataclasses, enums and containers into plain JSON-serializable data."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_serializable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _to_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_serializable(v) for v in obj]
    if isinstance(obj, enum.Enum):
        return obj.value
    return obj


def _strip_none(obj: Any) -> Any:
    """Recursively remove ``None`` values, mirroring Gson's default null handling."""
    if isinstance(obj, dict):
        return {k: _strip_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, (list, tuple)):
        return [_strip_none(v) for v in obj]
    return obj


def gson_dumps(obj: Any) -> str:
    """Serialize an object to JSON, mirroring the Gson behaviour used in Java.

    - ``None`` values are omitted (Gson default)
    - HTML characters are not escaped (``disableHtmlEscaping``)
    - Compact separators are used
    """
    data = _strip_none(_to_serializable(obj))
    dumped = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return (
        dumped.replace("\\u003c", "<")
        .replace("\\u003e", ">")
        .replace("\\u0026", "&")
    )


def gson_loads(data: str) -> Any:
    """Parse a JSON string, mirroring the Gson behaviour used in Java."""
    return json.loads(data)


__all__ = [
    "BEDROCK_PROTOCOL_VERSION",
    "BEDROCK_VERSION",
    "SERVICE_CONFIG_ID",
    "TEMPLATE_NAME",
    "TITLE_ID",
    "CREATE_SESSION",
    "JOIN_SESSION",
    "RTA_WEBSOCKET",
    "CREATE_HANDLE",
    "PEOPLE",
    "USER_PRESENCE",
    "FOLLOWERS",
    "SOCIAL",
    "SOCIAL_SUMMARY",
    "FOLLOWER",
    "GALLERY",
    "WEBSOCKET_CONNECTION_TIMEOUT",
    "CONNECTION_TYPE_JSON_RPC",
    "MAX_FRIENDS",
    "CONFIG_VERSION",
    "gson_dumps",
    "gson_loads",
    "quote",
]
