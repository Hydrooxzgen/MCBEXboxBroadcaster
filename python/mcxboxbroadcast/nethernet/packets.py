"""Minimal Bedrock protocol packet codecs for the redirect handshake.

Implements only the packets needed to complete the login handshake and then
transfer the client to the real server. Packet IDs target protocol version 786
(Minecraft 1.21.80).

The Java project uses the cloudburst Bedrock protocol library; here each packet
is encoded/decoded with plain ``struct`` operations. Frame framing (varint
length prefix) is handled by the NetherNet transport layer, matching how the
Bedrock protocol runs over NetherNet data channels.
"""

from __future__ import annotations

import struct
from typing import Any


# ---------------------------------------------------------------------------
# Varint / varlong helpers
# ---------------------------------------------------------------------------
def write_varint(value: int) -> bytes:
    value &= 0xFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def read_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError("Truncated varint")
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, offset
        shift += 7
        if shift > 35:
            raise ValueError("Varint too long")


def write_varlong(value: int) -> bytes:
    value &= 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def read_varlong(data: bytes, offset: int = 0) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError("Truncated varlong")
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, offset
        shift += 7
        if shift > 70:
            raise ValueError("Varlong too long")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
def write_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return write_varint(len(encoded)) + encoded


def read_string(data: bytes, offset: int = 0) -> tuple[str, int]:
    length, offset = read_varint(data, offset)
    end = offset + length
    if end > len(data):
        raise ValueError("Truncated string")
    return data[offset:end].decode("utf-8"), end


def write_unsigned_varint(value: int) -> bytes:
    return write_varint(value)


def write_zigzag32(value: int) -> bytes:
    return write_varint((value << 1) ^ (value >> 31))


def write_bool(value: bool) -> bytes:
    return bytes([1 if value else 0])


def write_byte(value: int) -> bytes:
    return struct.pack(">b", value)


def write_unsigned_byte(value: int) -> bytes:
    return struct.pack(">B", value)


def write_short(value: int) -> bytes:
    return struct.pack(">h", value)


def write_unsigned_short(value: int) -> bytes:
    return struct.pack(">H", value)


def write_float(value: float) -> bytes:
    return struct.pack(">f", value)


def write_little_float(value: float) -> bytes:
    return struct.pack("<f", value)


# ---------------------------------------------------------------------------
# Packet IDs (protocol 786 / 1.21.80)
# ---------------------------------------------------------------------------
PACKET_REQUEST_NETWORK_SETTINGS = 0xC1
PACKET_NETWORK_SETTINGS = 0x8F
PACKET_LOGIN = 0x01
PACKET_PLAY_STATUS = 0x02
PACKET_DISCONNECT = 0x05
PACKET_RESOURCE_PACKS_INFO = 0x06
PACKET_RESOURCE_PACK_STACK = 0x07
PACKET_RESOURCE_PACK_CLIENT_RESPONSE = 0x08
PACKET_START_GAME = 0x0B
PACKET_TRANSFER = 0x55
PACKET_CLIENT_CACHE_STATUS = 0x81

# PlayStatus values
PLAY_STATUS_LOGIN_SUCCESS = 0
PLAY_STATUS_LOGIN_FAILED_CLIENT_OLD = 1

# ResourcePackClientResponse statuses
RESOURCE_PACK_STATUS_HAVE_ALL_PACKS = 3
RESOURCE_PACK_STATUS_COMPLETED = 4


class BedrockPacket:
    packet_id: int = 0

    def encode_header(self) -> bytes:
        return write_varint(self.packet_id)

    def encode_payload(self) -> bytes:
        raise NotImplementedError

    def encode(self) -> bytes:
        return self.encode_header() + self.encode_payload()


class RequestNetworkSettingsPacket(BedrockPacket):
    packet_id = PACKET_REQUEST_NETWORK_SETTINGS

    def __init__(self, protocol_version: int = 0) -> None:
        self.protocol_version = protocol_version

    @classmethod
    def decode(cls, data: bytes) -> "RequestNetworkSettingsPacket":
        protocol, _ = read_varint(data)
        return cls(protocol)


class NetworkSettingsPacket(BedrockPacket):
    packet_id = PACKET_NETWORK_SETTINGS

    def __init__(self, compression_threshold: int = 0, compression_algorithm: int = 0) -> None:
        # 0 = zlib
        self.compression_threshold = compression_threshold
        self.compression_algorithm = compression_algorithm

    def encode_payload(self) -> bytes:
        return (
            write_unsigned_short(self.compression_threshold)
            + write_unsigned_byte(self.compression_algorithm)
        )


class LoginPacket(BedrockPacket):
    packet_id = PACKET_LOGIN

    def __init__(self, chain_data: bytes = b"", client_data_jwt: bytes = b"") -> None:
        self.chain_data = chain_data
        self.client_data_jwt = client_data_jwt

    @classmethod
    def decode(cls, data: bytes) -> "LoginPacket":
        offset = 0
        # Protocol version was moved out of login in newer versions, but keep the
        # reader tolerant of an optional first varint.
        chain_len, offset = read_varint(data, offset)
        chain_data = data[offset : offset + chain_len]
        offset += chain_len
        client_len, offset = read_varint(data, offset)
        client_data_jwt = data[offset : offset + client_len]
        return cls(chain_data, client_data_jwt)


class PlayStatusPacket(BedrockPacket):
    packet_id = PACKET_PLAY_STATUS

    def __init__(self, status: int = 0) -> None:
        self.status = status

    def encode_payload(self) -> bytes:
        return write_varint(self.status)


class DisconnectPacket(BedrockPacket):
    packet_id = PACKET_DISCONNECT

    def __init__(self, hide_disconnect_screen: bool = False, message: str = "") -> None:
        self.hide_disconnect_screen = hide_disconnect_screen
        self.message = message

    def encode_payload(self) -> bytes:
        return write_bool(self.hide_disconnect_screen) + write_string(self.message)


class ResourcePacksInfoPacket(BedrockPacket):
    packet_id = PACKET_RESOURCE_PACKS_INFO

    def __init__(self, world_template_id: str = "00000000-0000-0000-0000-000000000000") -> None:
        self.must_accept = False
        self.has_addons = False
        self.has_scripts = False
        self.force_server_packs = False
        self.world_template_id = world_template_id
        self.world_template_version = ""
        self.world_template_locked = False

    def encode_payload(self) -> bytes:
        out = bytearray()
        out += write_bool(self.must_accept)
        out += write_bool(self.has_addons)
        out += write_bool(self.has_scripts)
        out += write_bool(self.force_server_packs)
        # behaviour packs (count = 0)
        out += write_unsigned_short(0)
        # texture packs (count = 0)
        out += write_unsigned_short(0)
        # world template id / version
        out += write_string(self.world_template_id)
        out += write_string(self.world_template_version)
        # forced to accept false, experiments off, packs required off, vibrant visuals
        out += write_bool(self.world_template_locked)
        return bytes(out)


class ResourcePackStackPacket(BedrockPacket):
    packet_id = PACKET_RESOURCE_PACK_STACK

    def __init__(self) -> None:
        self.must_accept = False
        self.game_version = "*"
        self.experiments_previously_toggled = False

    def encode_payload(self) -> bytes:
        out = bytearray()
        out += write_bool(self.must_accept)
        # behaviour packs (count = 0)
        out += write_unsigned_short(0)
        # texture packs (count = 0)
        out += write_unsigned_short(0)
        # experiments (count = 0) + toggled flag
        out += write_unsigned_short(0)
        out += write_bool(self.experiments_previously_toggled)
        out += write_string(self.game_version)
        return bytes(out)


class ResourcePackClientResponsePacket(BedrockPacket):
    packet_id = PACKET_RESOURCE_PACK_CLIENT_RESPONSE

    def __init__(self, status: int = 0, pack_ids: list = None) -> None:
        self.status = status
        self.pack_ids = pack_ids or []

    @classmethod
    def decode(cls, data: bytes) -> "ResourcePackClientResponsePacket":
        status, offset = read_varint(data)
        count, offset = read_varint(data, offset)
        pack_ids = []
        for _ in range(count):
            pack_id, offset = read_string(data, offset)
            pack_ids.append(pack_id)
        return cls(status, pack_ids)


class StartGamePacket(BedrockPacket):
    packet_id = PACKET_START_GAME

    def __init__(self, session_manager: Any, session_info: Any) -> None:
        self.entity_id = 1
        self.runtime_entity_id = 1
        self.player_game_type = 1  # Creative
        self.player_position = (0.0, 66.0, 0.0)
        self.rotation = (1.0, 1.0)
        self.seed = 0
        self.dimension_id = 2
        self.generator_id = 1
        self.level_game_type = 1  # Creative
        self.difficulty = 0
        self.default_spawn = (0, 0, 0)
        self.achievements_disabled = True
        self.current_tick = -1
        self.multiplayer_game = True
        self.broadcasting_to_lan = True
        self.platform_broadcast_mode = 1  # Public
        self.xbox_live_broadcast_mode = 1  # Public
        self.show_coordinates = False
        self.editor_world = False
        self.permission_level = 1  # Member
        self.game_publish_setting = 1  # Public
        self.level_name = session_info.get_world_name()
        self.broadcast_settings = session_info.get_host_name()
        self.world_name = session_info.get_world_name()
        self.world_type = "flat" if False else "default"
        self.session_manager = session_manager

    def encode_payload(self) -> bytes:
        session_info = self.session_manager.session_info() if hasattr(self.session_manager, "session_info") else None
        out = bytearray()
        out += write_varlong(self.entity_id)
        out += write_varlong(self.runtime_entity_id)
        out += write_varint(self.player_game_type)
        out += write_little_float(self.player_position[0])
        out += write_little_float(self.player_position[1])
        out += write_little_float(self.player_position[2])
        out += write_little_float(self.rotation[0])
        out += write_little_float(self.rotation[1])
        out += write_varint(0)  # spawn settings
        out += write_varint(0)  # player property data (nbt length 0)
        out += write_varlong(self.seed)
        out += write_varint(self.dimension_id)
        out += write_varint(self.generator_id)
        out += write_varint(self.level_game_type)
        out += write_varint(self.difficulty)
        out += write_varint(self.default_spawn[0])
        out += write_varint(self.default_spawn[1])
        out += write_varint(self.default_spawn[2])
        out += write_bool(self.achievements_disabled)
        out += write_varint(self.current_tick)
        out += write_bool(self.multiplayer_game)
        out += write_bool(self.broadcasting_to_lan)
        out += write_varint(self.platform_broadcast_mode)
        out += write_varint(self.xbox_live_broadcast_mode)
        out += write_bool(False)  # commands enabled
        out += write_bool(False)  # texture packs required
        # gamerules
        out += write_varint(1)
        out += write_string("showcoordinates")
        out += write_bool(self.show_coordinates)
        out += write_bool(self.editor_world)
        out += write_bool(False)  # edu features
        out += write_bool(False)  # legacy edu
        out += write_string("")  # edu production id
        out += write_bool(False)  # experimental gameplay
        out += write_varint(0)  # experiments count
        out += write_string(self.world_name)
        out += write_varint(1)  # player permission member
        out += write_varint(self.game_publish_setting)
        out += write_varint(0)  # rain level
        out += write_varint(0)  # lightning level
        out += write_bool(False)  # has confirmed platform locked content
        out += write_bool(False)  # multiplayer game
        out += write_bool(False)  # LAN broadcast intent
        out += write_bool(False)  # xbox broadcast intent
        out += write_bool(False)  # platform broadcast intent
        out += write_bool(False)  # platform locked content
        out += write_bool(False)  # force experimental gameplay
        out += write_varint(0)  # server unique id (unsigned varint)
        out += write_varint(0)  # world template id (uuid is 16 bytes, 0 ok)
        out += write_string("")  # world template version
        # server engine & properties omitted for brevity (kept minimal)
        return bytes(out)


class TransferPacket(BedrockPacket):
    packet_id = PACKET_TRANSFER

    def __init__(self, address: str = "", port: int = 19132) -> None:
        self.address = address
        self.port = port

    def encode_payload(self) -> bytes:
        return write_string(self.address) + write_unsigned_short(self.port)


class ClientCacheStatusPacket(BedrockPacket):
    packet_id = PACKET_CLIENT_CACHE_STATUS

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled

    @classmethod
    def decode(cls, data: bytes) -> "ClientCacheStatusPacket":
        return cls(bool(data[0]))


__all__ = [
    "PACKET_REQUEST_NETWORK_SETTINGS",
    "PACKET_NETWORK_SETTINGS",
    "PACKET_LOGIN",
    "PACKET_PLAY_STATUS",
    "PACKET_DISCONNECT",
    "PACKET_RESOURCE_PACKS_INFO",
    "PACKET_RESOURCE_PACK_STACK",
    "PACKET_RESOURCE_PACK_CLIENT_RESPONSE",
    "PACKET_START_GAME",
    "PACKET_TRANSFER",
    "PACKET_CLIENT_CACHE_STATUS",
    "PLAY_STATUS_LOGIN_SUCCESS",
    "PLAY_STATUS_LOGIN_FAILED_CLIENT_OLD",
    "RESOURCE_PACK_STATUS_HAVE_ALL_PACKS",
    "RESOURCE_PACK_STATUS_COMPLETED",
    "RequestNetworkSettingsPacket",
    "NetworkSettingsPacket",
    "LoginPacket",
    "PlayStatusPacket",
    "DisconnectPacket",
    "ResourcePacksInfoPacket",
    "ResourcePackStackPacket",
    "ResourcePackClientResponsePacket",
    "StartGamePacket",
    "TransferPacket",
    "ClientCacheStatusPacket",
]
