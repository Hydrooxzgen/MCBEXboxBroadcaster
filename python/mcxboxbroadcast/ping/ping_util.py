"""Bedrock server pinging, mirroring ``core/ping``.

The Java implementation uses the cloudburst RakNet client; here we implement the
small subset of RakNet we need (unconnected ping/pong) over plain UDP sockets,
with the same GeyserMC web API fallback.

RakNet protocol reference:
https://wiki.vg/Raknet_Protocol
"""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

# MAGIC value used by RakNet ("00ffff00fefefefefdfdfdfd12345678")
RAKNET_MAGIC = bytes.fromhex("00ffff00fefefefefdfdfdfd12345678")

# Packet IDs
ID_UNCONNECTED_PING = 0x01
ID_UNCONNECTED_PONG = 0x1C


@dataclass
class BedrockPong:
    """Parsed Bedrock MOTD pong data (key=value pairs separated by ';')."""

    edition: str = "MCPE"
    motd1: str = ""
    protocol: str = "0"
    version: str = ""
    player_count: str = "0"
    max_player_count: str = "0"
    server_id: str = ""
    motd2: str = ""
    game_mode: str = ""
    game_mode_id: str = "0"
    ipv4_port: str = "0"
    ipv6_port: str = "0"
    extra: Optional[list] = None

    @classmethod
    def from_raknet(cls, data: bytes) -> "BedrockPong":
        if isinstance(data, str):
            text = data
        else:
            text = data.decode("utf-8", errors="replace")
        parts = text.split(";")
        pong = cls()
        if len(parts) > 0:
            pong.edition = parts[0]
        if len(parts) > 1:
            pong.motd1 = parts[1]
        if len(parts) > 2:
            pong.protocol = parts[2]
        if len(parts) > 3:
            pong.version = parts[3]
        if len(parts) > 4:
            pong.player_count = parts[4]
        if len(parts) > 5:
            pong.max_player_count = parts[5]
        if len(parts) > 6:
            pong.server_id = parts[6]
        if len(parts) > 7:
            pong.motd2 = parts[7]
        if len(parts) > 8:
            pong.game_mode = parts[8]
        if len(parts) > 9:
            pong.game_mode_id = parts[9]
        if len(parts) > 10:
            pong.ipv4_port = parts[10]
        if len(parts) > 11:
            pong.ipv6_port = parts[11]
        if len(parts) > 12:
            pong.extra = parts[12:]
        return pong


class PingException(Exception):
    pass


def _build_unconnected_ping() -> bytes:
    """Build a RakNet unconnected ping packet (no client GUID, like the Java impl)."""
    packet = bytearray()
    packet.append(ID_UNCONNECTED_PING)
    packet += struct.pack(">q", int(time.time() * 1000))
    packet += RAKNET_MAGIC
    # No client GUID is appended by the Java RakPing
    return bytes(packet)


def _parse_unconnected_pong(data: bytes) -> bytes:
    """Validate an unconnected pong and return the raw pong data."""
    if len(data) < 35:
        raise PingException("Pong response too short")
    offset = 1
    ping_time = struct.unpack(">q", data[offset : offset + 8])[0]
    offset += 8
    if data[offset : offset + 16] != RAKNET_MAGIC:
        raise PingException("Invalid RakNet magic in pong response")
    offset += 16
    # Server GUID (8 bytes)
    offset += 8
    # Remaining data is the MOTD string (uint16 length prefixed)
    (length,) = struct.unpack(">H", data[offset : offset + 2])
    offset += 2
    return data[offset : offset + length]


def raknet_ping(host: str, port: int, timeout: float = 3.0) -> BedrockPong:
    """Ping a Bedrock server over UDP and return the parsed pong.

    Raises :class:`PingException` on any failure.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(_build_unconnected_ping(), (host, port))
        data, _addr = sock.recvfrom(2048)
    except (socket.timeout, OSError) as e:
        raise PingException(f"Failed to ping server: {e}") from e
    finally:
        sock.close()

    pong_data = _parse_unconnected_pong(data)
    return BedrockPong.from_raknet(pong_data)


def web_ping(host: str, port: int, timeout: float = 3.0) -> BedrockPong:
    """Fall back to the GeyserMC checker API to ping the server."""
    try:
        response = requests.get(
            f"https://checker.geysermc.org/ping?hostname={host}&port={port}",
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise PingException(f"WebAPI: Failed to ping server: {e}") from e

    if response.status_code != 200:
        raise PingException("WebAPI: Failed to ping server")

    data = response.json()
    if not data.get("success"):
        raise PingException("WebAPI: Server is offline")

    pong_data = data.get("ping", {}).get("pong")
    if isinstance(pong_data, dict):
        return BedrockPong(
            edition=pong_data.get("edition", "MCPE"),
            motd1=pong_data.get("motd1", ""),
            protocol=str(pong_data.get("protocol", "0")),
            version=pong_data.get("version", ""),
            player_count=str(pong_data.get("playerCount", "0")),
            max_player_count=str(pong_data.get("maxPlayerCount", "0")),
            server_id=pong_data.get("serverId", ""),
            motd2=pong_data.get("motd2", ""),
            game_mode=pong_data.get("gameMode", ""),
            game_mode_id=str(pong_data.get("gameModeId", "0")),
            ipv4_port=str(pong_data.get("ipv4Port", "0")),
            ipv6_port=str(pong_data.get("ipv6Port", "0")),
        )
    if isinstance(pong_data, str):
        return BedrockPong.from_raknet(pong_data.encode("utf-8"))
    raise PingException("WebAPI: Unexpected pong format")


def ping(host: str, port: int, timeout: float = 3.0, web_fallback: bool = False) -> BedrockPong:
    """Ping a Bedrock server, optionally falling back to the web API."""
    try:
        return raknet_ping(host, port, timeout)
    except PingException as e:
        if web_fallback:
            return web_ping(host, port, timeout)
        raise


__all__ = ["BedrockPong", "PingException", "raknet_ping", "web_ping", "ping"]
