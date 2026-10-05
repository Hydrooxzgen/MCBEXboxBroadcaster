"""RakNet ping utilities, ported from Java PingUtil.java (GPL-2.0, WaterdogPE
derived, like the original).

Implements the Bedrock RakNet unconnected ping/pong over raw UDP, with the
optional checker.geysermc.org web fallback.
"""

from __future__ import annotations

import json
import random
import socket
import struct
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Optional

import requests

from ..logger import Logger

logger = Logger("Ping")

MAGIC = bytes(
    [
        0x00, 0xFF, 0xFF, 0x00, 0xFE, 0xFE, 0xFE, 0xFE,
        0xFD, 0xFD, 0xFD, 0xFD, 0x12, 0x34, 0x56, 0x78,
    ]
)

_web_ping_enabled = False


def set_web_ping_enabled(enabled: bool) -> None:
    global _web_ping_enabled
    _web_ping_enabled = enabled


@dataclass
class BedrockPong:
    gamertag: str = ""  # not used in pong but kept for parity
    hostname: str = ""
    sub_motd: str = ""
    motd: str = ""
    player_count: int = 0
    maximum_player_count: int = 0
    protocol_version: int = 0
    version: str = ""
    game_type: str = "Survival"
    ipv4_port: int = 0
    ipv6_port: int = 0
    nintendo: bool = False
    guid: int = 0


def ping(host: str, port: int, timeout_ms: int = 1500) -> Future:
    """Ping a bedrock server; returns a Future with a BedrockPong or raises."""
    future: Future = Future()

    def run() -> None:
        try:
            pong = _raknet_ping(host, port, timeout_ms)
            future.set_result(pong)
        except Exception as raknet_ex:
            if _web_ping_enabled:
                try:
                    future.set_result(_web_ping(host, port, timeout_ms / 1000.0))
                except Exception as web_ex:
                    future.set_exception(web_ex)
            else:
                future.set_exception(raknet_ex)

    threading.Thread(target=run, daemon=True, name="MCXboxBroadcast-Ping").start()
    return future


def _raknet_ping(host: str, port: int, timeout_ms: int) -> BedrockPong:
    guid = random.getrandbits(64)
    ping_packet = bytes([0x01]) + struct.pack("<Q", _now_ms()) + MAGIC + struct.pack("<Q", guid)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout_ms / 1000.0)
    try:
        sock.sendto(ping_packet, (host, port))
        while True:
            data, _addr = sock.recvfrom(2048)
            if len(data) < 35 or data[0] != 0x1C:
                continue
            # 0x1c + time(8 LE) + serverGUID(8 LE) + magic(16) + string
            if data[17:33] != MAGIC:
                continue
            return _parse_pong(data[33:])
    finally:
        sock.close()


def _parse_pong(payload: bytes) -> BedrockPong:
    if len(payload) < 2:
        raise IOError("Pong payload too short")
    (length,) = struct.unpack(">H", payload[:2])
    text = payload[2 : 2 + length].decode("utf-8", errors="replace")
    parts = text.split(";")
    if len(parts) < 8:
        raise IOError(f"Malformed pong data: {text[:100]}")
    return BedrockPong(
        hostname=parts[0],
        motd=parts[1],
        protocol_version=int(parts[2] or 0),
        version=parts[3],
        player_count=_to_int(parts[4]),
        maximum_player_count=_to_int(parts[5]),
        guid=_to_int(parts[6]) or 0,
        sub_motd=parts[7] if len(parts) > 7 else "",
        game_type=parts[8] if len(parts) > 8 and parts[8] in ("Survival", "Creative", "Adventure") else "Survival",
        ipv4_port=_to_int(parts[10]) if len(parts) > 10 else (_to_int(parts[8]) if len(parts) > 8 else 0),
        ipv6_port=_to_int(parts[11]) if len(parts) > 11 else (_to_int(parts[9]) if len(parts) > 9 else 0),
        nintendo=len(parts) > 9 and parts[9] == "1",
    )


def _to_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _web_ping(host: str, port: int, timeout: float) -> BedrockPong:
    response = requests.get(
        f"https://checker.geysermc.org/ping?hostname={host}&port={port}", timeout=timeout
    )
    if response.status_code != 200:
        raise IOError("WebAPI: Failed to ping server")
    data = response.json()
    if not data.get("success"):
        raise IOError("WebAPI: Server is offline")
    pong_data = data.get("ping", {}).get("pong")
    if not pong_data:
        raise IOError("WebAPI: Missing pong data")
    return BedrockPong(
        hostname=str(pong_data.get("hostname", "")),
        motd=str(pong_data.get("motd", "")),
        sub_motd=str(pong_data.get("subMotd", "")),
        player_count=_to_int(str(pong_data.get("playerCount", 0))),
        maximum_player_count=_to_int(str(pong_data.get("maximumPlayerCount", 0))),
        protocol_version=_to_int(str(pong_data.get("protocolVersion", 0))),
        version=str(pong_data.get("version", "")),
        game_type=str(pong_data.get("gameType", "Survival")),
        guid=_to_int(str(pong_data.get("guid", 0))),
        ipv4_port=_to_int(str(pong_data.get("ipv4Port", 0))),
        ipv6_port=_to_int(str(pong_data.get("ipv6Port", 0))),
    )


def _now_ms() -> int:
    import time

    return int(time.time() * 1000) & 0xFFFFFFFFFFFFFFFF
