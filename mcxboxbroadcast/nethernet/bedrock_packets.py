"""Minimal Bedrock packet layer for protocol 2169 (1.26.45).

Only the packets needed for the NetherNet redirect flow are implemented,
with field orders taken from CloudburstMC Protocol (Bedrock_v2168/v2169 and
their serializer chain). See PORTING_NOTES.md section 4.

Framing on the NetherNet data channel:
  message = segment_count byte + payload chunks
  reassembled payload = varuint32(length) + frame, repeated
  frame = compression byte (0x00 zlib-raw / 0xff none) + compressed body
  body = varuint32(header: packet id) + packet data
"""

from __future__ import annotations

import json
import struct
import uuid
import zlib
from typing import Optional, Tuple

from .. import constants

# Packet IDs (protocol 2169)
PACKET_LOGIN = 1
PACKET_PLAY_STATUS = 2
PACKET_DISCONNECT = 3
PACKET_RESOURCE_PACKS_INFO = 6
PACKET_RESOURCE_PACK_STACK = 7
PACKET_RESOURCE_PACK_CLIENT_RESPONSE = 8
PACKET_START_GAME = 11
PACKET_TRANSFER = 85
PACKET_CLIENT_CACHE_STATUS = 129
PACKET_NETWORK_SETTINGS = 143
PACKET_REQUEST_NETWORK_SETTINGS = 193

# PlayStatus enum ordinals
PLAY_STATUS_LOGIN_SUCCESS = 0
PLAY_STATUS_LOGIN_FAILED_CLIENT_OLD = 0  # real value set below
# PlayStatus.Status ordinal order (CloudburstMC):
# LOGIN_SUCCESS, FAILED_CLIENT, FAILED_CLIENT_OLD, FAILED_SERVER,
# PLAYER_SPAWN, FAILED_INVALID_TENANT, ...
PLAY_STATUS_FAILED_CLIENT_OLD = 2

# ResourcePackClientResponse.Status enum order (CloudburstMC):
# REFUSED=0, SEND_PACKS=1, HAVE_ALL_PACKS=2, COMPLETED=3 (values are written -1)
RPC_STATUS_HAVE_ALL_PACKS = 2
RPC_STATUS_COMPLETED = 3

COMPRESSION_ZLIB = 0x00
COMPRESSION_NONE = 0xFF


# ---------------------------------------------------------------- primitives
def write_varuint(buf: bytearray, value: int) -> None:
    value &= 0xFFFFFFFFFFFFFFFF
    while True:
        if (value & ~0x7F) == 0:
            buf.append(value)
            return
        buf.append((value & 0x7F) | 0x80)
        value >>= 7


def read_varuint(data: bytes, offset: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError("varint out of bounds")
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result & 0xFFFFFFFFFFFFFFFF, offset
        shift += 7
        if shift >= 70:
            raise ValueError("varint too long")


def write_varint(buf: bytearray, value: int) -> None:
    # zigzag for signed
    write_varuint(buf, ((value << 1) ^ (value >> 31)) & 0xFFFFFFFF)


def read_varint(data: bytes, offset: int) -> Tuple[int, int]:
    raw, offset = read_varuint(data, offset)
    value = raw & 0xFFFFFFFF
    return (value >> 1) ^ -(value & 1), offset


def write_string(buf: bytearray, text: str) -> None:
    encoded = text.encode("utf-8")
    write_varuint(buf, len(encoded))
    buf += encoded


def read_string(data: bytes, offset: int) -> Tuple[str, int]:
    length, offset = read_varuint(data, offset)
    text = data[offset : offset + length].decode("utf-8", errors="replace")
    return text, offset + length


def write_uuid(buf: bytearray, value: uuid.UUID) -> None:
    buf += struct.pack("<Q", value.int >> 64)
    buf += struct.pack("<Q", value.int & 0xFFFFFFFFFFFFFFFF)


# ------------------------------------------------------------------ framing
def _encode_batch_inner(bodies: list[bytes]) -> bytes:
    payload = bytearray()
    for body in bodies:
        write_varuint(payload, len(body))
        payload += body
    return bytes(payload)


def _deflate(data: bytes) -> bytes:
    # Bedrock compression is RAW deflate (no zlib wrapper)
    co = zlib.compressobj(6, zlib.DEFLATED, -15)
    return co.compress(data) + co.flush()


def _inflate(data: bytes) -> bytes:
    return zlib.decompress(data, -15)


def encode_batch(bodies: list[bytes], compressed: bool = False) -> bytes:
    """Encode packet bodies into one data-channel payload.

    Compressed layout: [0x00][raw-deflate(varuint len + body)...]
    Uncompressed layout: [varuint len + body]...
    """
    inner = _encode_batch_inner(bodies)
    if compressed:
        return bytes([COMPRESSION_ZLIB]) + _deflate(inner)
    return inner


def compress_frame(packet_bytes: bytes, threshold: int = 1) -> bytes:
    if len(packet_bytes) > threshold:
        return bytes([COMPRESSION_ZLIB]) + _deflate(packet_bytes)
    return bytes([COMPRESSION_NONE]) + packet_bytes


def decompress_frame(frame: bytes) -> bytes:
    header = frame[0]
    body = frame[1:]
    if header == COMPRESSION_ZLIB:
        return _inflate(body)
    if header == COMPRESSION_NONE:
        return body
    raise ValueError(f"Unknown compression algorithm header: {header:#x}")


def decode_payload(payload: bytes, compressed: bool = True) -> list[bytes]:
    """Decode one assembled data-channel payload into packet bodies.

    Compressed frames carry the compression header byte BEFORE the length
    prefix (matches the real client and kastle). A trailing partial batch
    (client batching across compression switch) is tolerated.
    """
    if compressed:
        if not payload:
            return []
        header = payload[0]
        data = payload[1:]
        if header == COMPRESSION_ZLIB:
            data = _inflate(data)
        elif header == COMPRESSION_NONE:
            pass
        else:
            raise ValueError(f"Unknown compression algorithm header: {header:#x}")
    else:
        data = payload

    out = []
    offset = 0
    while offset < len(data):
        try:
            length, next_offset = read_varuint(data, offset)
        except ValueError:
            break
        if next_offset + length > len(data):
            break  # incomplete trailing data
        out.append(data[next_offset : next_offset + length])
        offset = next_offset + length
    return out


def encode_packet_body(packet_id: int, body: bytes) -> bytes:
    header = bytearray()
    write_varuint(header, packet_id)
    return bytes(header) + body


def decode_packet_body(body: bytes) -> Tuple[int, bytes]:
    packet_id, offset = read_varuint(body, 0)
    return packet_id, body[offset:]


# ------------------------------------------------------- outbound serializers
def network_settings(threshold: int = 0) -> bytes:
    buf = bytearray()
    buf += struct.pack("<H", threshold)  # compressionThreshold
    buf += struct.pack("<H", 0)  # compressionAlgorithm ordinal (ZLIB=0)
    buf += struct.pack("B", 0)  # clientThrottleEnabled
    buf += struct.pack("B", 0)  # clientThrottleThreshold
    buf += struct.pack("<f", 0.0)  # clientThrottleScalar
    return encode_packet_body(PACKET_NETWORK_SETTINGS, bytes(buf))


def play_status(status: int) -> bytes:
    return encode_packet_body(PACKET_PLAY_STATUS, struct.pack(">i", status))


def disconnect(message: Optional[str] = None) -> bytes:
    buf = bytearray()
    if message is None:
        buf.append(1)  # messageSkipped
    else:
        buf.append(0)
        write_string(buf, message)
    return encode_packet_body(PACKET_DISCONNECT, bytes(buf))


def resource_packs_info() -> bytes:
    buf = bytearray()
    buf.append(0)  # forcedToAccept=false
    buf.append(0)  # hasAddonPacks=false
    buf.append(0)  # scriptingEnabled=false
    buf.append(1)  # vibrantVisualsForceDisabled=true
    write_uuid(buf, uuid.UUID(int=0))
    write_string(buf, "")  # worldTemplateVersion
    write_varuint(buf, 0)  # resource pack entry count
    return encode_packet_body(PACKET_RESOURCE_PACKS_INFO, bytes(buf))


def resource_pack_stack() -> bytes:
    buf = bytearray()
    buf.append(0)  # forcedToAccept=false
    write_varuint(buf, 0)  # resource pack entry count
    write_string(buf, "*")  # gameVersion
    buf += struct.pack("<i", 0)  # experiments count (writeIntLE)
    buf.append(0)  # experimentsPreviouslyToggled=false
    buf.append(0)  # hasEditorPacks=false
    return encode_packet_body(PACKET_RESOURCE_PACK_STACK, bytes(buf))


def transfer(address: str, port: int) -> bytes:
    buf = bytearray()
    write_string(buf, address)
    buf += struct.pack("<h", port)
    buf.append(0)  # reloadWorld=false
    buf.append(0)  # gatheringsConfigurationJoinInfo optional: absent
    return encode_packet_body(PACKET_TRANSFER, bytes(buf))


def start_game(
    host_name: str,
    world_name: str,
    players: int,
    max_players: int,
    protocol: int = constants.PROTOCOL_VERSION,
    version: str = constants.MINECRAFT_VERSION,
) -> bytes:
    buf = bytearray()

    # header (v428 serialize)
    _write_varlong(buf, 1)  # uniqueEntityId (varlong)
    write_varuint(buf, 1)  # runtimeEntityId
    write_varint(buf, 1)  # playerGameType = CREATIVE
    buf += struct.pack("<fff", 0.0, 66.0, 0.0)  # playerPosition
    buf += struct.pack("<ff", 1.0, 1.0)  # rotation

    # ---- writeLevelSettings (v2168) ----
    buf += struct.pack("<q", 0)  # seed (int64 LE, v527+)
    buf += struct.pack("<H", 0)  # spawnBiomeType = DEFAULT
    write_string(buf, "")  # customBiomeName
    write_varint(buf, 2)  # dimensionId = 2
    write_varint(buf, 1)  # generatorId = 1
    write_varint(buf, 1)  # levelGameType = CREATIVE
    buf.append(0)  # hardcore = false
    write_varint(buf, 0)  # difficulty = 0
    write_varint(buf, 0)  # defaultSpawn x
    write_varuint(buf, 0)  # defaultSpawn y
    write_varint(buf, 0)  # defaultSpawn z
    buf.append(1)  # achievementsDisabled = true
    write_varint(buf, 0)  # editorWorldType = NON_EDITOR
    buf.append(0)  # createdInEditor = false
    buf.append(0)  # exportedFromEditor = false
    write_varint(buf, -1)  # dayCycleStopTime = -1
    write_varuint(buf, 0)  # eduEditionOffers = 0
    buf.append(0)  # eduFeaturesEnabled = false
    write_string(buf, "")  # educationProductionId
    buf += struct.pack("<f", 0.0)  # rainLevel
    buf += struct.pack("<f", 0.0)  # lightningLevel
    buf.append(0)  # platformLockedContentConfirmed = false
    buf.append(1)  # multiplayerGame = true
    buf.append(1)  # broadcastingToLan = true
    write_varint(buf, 4)  # xblBroadcastMode = PUBLIC (GamePublishSetting ordinal 4)
    write_varint(buf, 4)  # platformBroadcastMode = PUBLIC
    buf.append(1)  # commandsEnabled = true
    buf.append(0)  # texturePacksRequired = false
    write_varuint(buf, 0)  # gamerules: empty array
    buf += struct.pack("<i", 0)  # experiments: empty array (writeIntLE)
    buf.append(0)  # experimentsPreviouslyToggled = false
    buf.append(0)  # bonusChestEnabled = false
    buf.append(0)  # startingWithMap = false
    buf.append(0)  # defaultPlayerPermission = VISITOR (byte)
    buf += struct.pack("<i", 4)  # serverChunkTickRange (int LE)
    buf.append(0)  # behaviorPackLocked = false
    buf.append(0)  # resourcePackLocked = false
    buf.append(0)  # fromLockedWorldTemplate = false
    buf.append(0)  # usingMsaGamertagsOnly = false
    buf.append(0)  # fromWorldTemplate = false
    buf.append(0)  # worldTemplateOptionLocked = false
    buf.append(0)  # onlySpawningV1Villagers = false
    buf.append(0)  # disablingPersonas = false
    buf.append(0)  # disablingCustomSkins = false
    buf.append(0)  # emoteChatMuted = false
    write_string(buf, "*")  # vanillaVersion
    buf += struct.pack("<i", 16)  # limitedWorldWidth (int LE)
    buf += struct.pack("<i", 16)  # limitedWorldHeight (int LE)
    buf.append(0)  # netherType = false
    write_string(buf, "")  # eduSharedUriResource buttonName
    write_string(buf, "")  # eduSharedUriResource linkUri
    buf.append(0)  # forceExperimentalGameplay optional: absent
    buf.append(0)  # chatRestrictionLevel = NONE (byte)
    buf.append(0)  # disablingPlayerInteractions = false
    write_varint(buf, 0)  # serverEditorConnectionPolicy (v1001)
    buf.append(1)  # allowAnonymousBlockDropsInEditorWorlds = true

    # ---- v428 serialize tail ----
    write_string(buf, "")  # levelId
    write_string(buf, "MCXboxBroadcast")  # levelName
    write_string(buf, "")  # premiumWorldTemplateId
    buf.append(0)  # trial = false
    write_varint(buf, 0)  # rewindHistorySize (v818 movement settings)
    buf.append(0)  # serverAuthoritativeBlockBreaking = false
    buf += struct.pack("<q", 0)  # currentTick (int64 LE)
    write_varint(buf, 0)  # enchantmentSeed
    write_varuint(buf, 0)  # blockProperties: empty array
    # itemDefinitions: noop since v776
    write_string(buf, "")  # multiplayerCorrelationId
    buf.append(0)  # inventoriesServerAuthoritative = false
    write_string(buf, "")  # serverEngine (v440)

    # ---- v527 additions ----
    # playerPropertyData: empty compound; network NBT root tag carries an
    # (empty) name, so type byte + name length varint(0) + end tag
    buf += b"\x0a\x00\x00"
    buf += struct.pack("<Q", 0)  # blockRegistryChecksum (int64 LE)
    write_uuid(buf, uuid.UUID(int=0))  # worldTemplateId

    # ---- v582 / v544 / v827(v898) additions ----
    buf.append(0)  # blockNetworkIdsHashed = false
    buf.append(0)  # clientSideGenerationEnabled = false
    buf.append(0)  # tickDeathSystemsEnabled = false
    buf.append(0)  # networkPermissions.serverAuthSounds = false

    # ---- v924 additions ----
    buf.append(0)  # serverConfigurationJoinInfo optional: absent
    write_string(buf, "")  # serverId
    write_string(buf, "")  # scenarioId
    write_string(buf, "")  # worldId
    write_string(buf, "")  # ownerId

    return encode_packet_body(PACKET_START_GAME, bytes(buf))


# ------------------------------------------------------- inbound deserializers
def parse_request_network_settings(body: bytes) -> int:
    # Netty writeInt() is big endian
    (protocol,) = struct.unpack_from(">i", body, 0)
    return protocol


def parse_login(body: bytes) -> Tuple[int, str, str]:
    """Returns (protocol, authPayloadJson, clientJwt)."""
    (protocol,) = struct.unpack_from(">i", body, 0)
    offset = 4
    total_len, offset = read_varuint(body, offset)
    end = offset + total_len
    (auth_len,) = struct.unpack_from("<i", body, offset)
    offset += 4
    auth_jwt = body[offset : offset + auth_len].decode("utf-8", errors="replace")
    offset += auth_len
    (client_len,) = struct.unpack_from("<i", body, offset)
    offset += 4
    client_jwt = body[offset : offset + client_len].decode("utf-8", errors="replace")
    return protocol, auth_jwt, client_jwt


def parse_auth_payload(auth_jwt: str) -> Tuple[str, str, str]:
    """Parse the login auth payload (modern and legacy formats).

    Returns (kind, token_or_chain_json, identity_key_b64):
      kind == "token"  -> token_or_chain_json is the Mojang RS256 token JWT
      kind == "chain"  -> token_or_chain_json is the raw JSON, identity key
                          resolved by the legacy chain validator
    """
    payload = json.loads(auth_jwt)
    cert = payload.get("Certificate")
    token = payload.get("Token")
    if token:
        return "token", str(token), ""
    if cert:
        chain_json = json.loads(cert)
        chain = chain_json.get("chain")
        if isinstance(chain, list) and chain:
            return "chain", auth_jwt, ""
    raise ValueError("Login auth payload contains no Token or Certificate chain")


def parse_resource_pack_client_response(body: bytes) -> Tuple[int, str]:
    status_raw, offset = read_varuint(body, 0)
    status = status_raw + 1
    response_type, offset = read_string(body, offset)
    return status, response_type


def parse_login_chain(auth_jwt: str, client_jwt: str) -> Tuple[bool, Optional[dict]]:
    """Validate the Mojang-signed login chain and the client data JWT.

    Trust model: chain[0] is signed by Mojang's root key and carries the
    identity public key + extraData; chain[1] (client data) is signed with
    that identity key.

    Returns (signed_ok, extra_data).
    """
    import base64

    from cryptography.hazmat.primitives import serialization

    from ..auth.crypto import verify_es256_jws
    from ..auth.mojang import MOJANG_ROOT_PUBLIC_KEY_B64

    def b64_decode(text: str) -> bytes:
        return base64.b64decode(text)

    try:
        chain = json.loads(auth_jwt).get("chain")
        if not isinstance(chain, list) or len(chain) != 2:
            return False, None

        # chain[0]: signed by the Mojang root key
        header0 = json.loads(_b64url_decode(chain[0].split(".")[0]))
        x5u0 = header0.get("x5u")
        if not x5u0 or b64_decode(x5u0) != b64_decode(MOJANG_ROOT_PUBLIC_KEY_B64):
            return False, None
        if not verify_es256_jws(chain[0], b64_decode(x5u0)):
            return False, None

        payload0 = json.loads(_b64url_decode(chain[0].split(".")[1]))
        identity_key_b64 = payload0.get("identityPublicKey")
        extra_data = payload0.get("extraData")
        if not identity_key_b64:
            return False, None

        # chain[1]: signed by the identity key from chain[0]
        header1 = json.loads(_b64url_decode(chain[1].split(".")[0]))
        x5u1 = header1.get("x5u")
        if not x5u1 or b64_decode(x5u1) != b64_decode(identity_key_b64):
            return False, None
        if not verify_es256_jws(chain[1], b64_decode(x5u1)):
            return False, None

        # client data JWT: also signed with the identity key
        if not verify_es256_jws(client_jwt, b64_decode(identity_key_b64)):
            return False, None

        return True, extra_data
    except Exception:
        return False, None


def _b64url_decode(text: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _root_key_der() -> bytes:
    from cryptography.hazmat.primitives import serialization

    from ..auth import mojang_root_key_pem

    key = serialization.load_pem_public_key(mojang_root_key_pem().encode())
    return key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _write_varlong(buf: bytearray, value: int) -> None:
    # Zigzag encode the signed 64-bit value, then emit as an unsigned varint
    zigzag = ((value << 1) ^ (value >> 63)) & 0xFFFFFFFFFFFFFFFF
    write_varuint(buf, zigzag)
