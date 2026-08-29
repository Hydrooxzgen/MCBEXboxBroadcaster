"""Bedrock redirect packet handler, mirroring ``RedirectPacketHandler``.

Runs the login handshake against an incoming Minecraft client and, once the
client reports all resource packs are loaded, sends a ``StartGame`` followed by
a ``Transfer`` packet pointing at the real server.

The Java version also validates the login chain and client data JWT signatures;
here the chain is parsed and validated structurally, with full public-key
signature verification left as a TODO (see ``verify`` below).
"""

from __future__ import annotations

import base64
import json
from typing import Optional

from mcxboxbroadcast.constants import BEDROCK_PROTOCOL_VERSION
from mcxboxbroadcast.logger import Logger
from mcxboxbroadcast.nethernet.packets import (
    PLAY_STATUS_LOGIN_FAILED_CLIENT_OLD,
    PLAY_STATUS_LOGIN_SUCCESS,
    RESOURCE_PACK_STATUS_COMPLETED,
    RESOURCE_PACK_STATUS_HAVE_ALL_PACKS,
    ClientCacheStatusPacket,
    DisconnectPacket,
    LoginPacket,
    NetworkSettingsPacket,
    PlayStatusPacket,
    RequestNetworkSettingsPacket,
    ResourcePackClientResponsePacket,
    ResourcePackStackPacket,
    StartGamePacket,
    TransferPacket,
    write_varint,
)

# Compression algorithm: 0 = zlib
ZLIB = 0


def _b64decode_jwt(segment: str) -> dict:
    padding = "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment + padding).decode("utf-8"))


class RedirectPacketHandler:
    def __init__(self, session_manager: "object", session_info: "object", logger: Logger) -> None:
        self._session_manager = session_manager
        self._session_info = session_info
        self._logger = logger.prefixed("NetherNet")

        self._identity_data: Optional[dict] = None
        self._network_settings_requested = False

    # ------------------------------------------------------------------
    # Transport-facing API (called by the NetherNet connection)
    # ------------------------------------------------------------------
    def handle_frame(self, frame: bytes) -> Optional[bytes]:
        """Handle one incoming packet frame; returns the response frame(s) to send.

        ``frame`` is the raw packet payload (varint packet id + payload).
        Returns ``None`` when nothing should be sent.
        """
        from mcxboxbroadcast.nethernet.packets import read_varint

        packet_id, offset = read_varint(frame)
        payload = frame[offset:]

        if packet_id == 0xC1:  # RequestNetworkSettings
            return self._handle_request_network_settings(RequestNetworkSettingsPacket.decode(payload))
        if packet_id == 0x01:  # Login
            return self._handle_login(LoginPacket.decode(payload))
        if packet_id == 0x81:  # ClientCacheStatus
            ClientCacheStatusPacket.decode(payload)
            return None
        if packet_id == 0x08:  # ResourcePackClientResponse
            return self._handle_resource_pack_client_response(
                ResourcePackClientResponsePacket.decode(payload)
            )
        # Unknown packets are ignored, mirroring the Java behaviour
        return None

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------
    def _handle_request_network_settings(self, packet: RequestNetworkSettingsPacket) -> Optional[bytes]:
        client_protocol = packet.protocol_version
        server_protocol = BEDROCK_PROTOCOL_VERSION

        if client_protocol != server_protocol:
            message = (
                "disconnectionScreen.outdatedServer"
                if client_protocol > server_protocol
                else "disconnectionScreen.outdatedClient"
            )
            return self._disconnect(message)

        settings = NetworkSettingsPacket(
            compression_threshold=0, compression_algorithm=ZLIB
        )
        self._network_settings_requested = True
        return settings.encode()

    def _handle_login(self, packet: LoginPacket) -> Optional[bytes]:
        if not self._network_settings_requested:
            status = PlayStatusPacket(PLAY_STATUS_LOGIN_FAILED_CLIENT_OLD)
            return status.encode() + self._disconnect()

        chain_data = packet.chain_data
        try:
            chain_payload = json.loads(chain_data.decode("utf-8"))
            chain_jwts = chain_payload.get("chain", [])
            if not chain_jwts:
                raise ValueError("Chain is empty")
            identity_claims = _b64decode_jwt(chain_jwts[-1].split(".")[1])
            extra_data = identity_claims.get("extraData")
            if not extra_data:
                raise ValueError("Chain has no extraData")
            self._identity_data = extra_data

            # Verify the client data JWT is signed by the identity public key.
            # TODO: implement ECDSA verification of the chain signatures.
            client_data = _b64decode_jwt(packet.client_data_jwt.decode("utf-8").split(".")[1])
            if not client_data:
                raise ValueError("Client data is invalid")
        except Exception as error:
            self._logger.debug(f"Failed to validate login packet: {error}")
            return self._disconnect("disconnect.loginFailed")

        status = PlayStatusPacket(PLAY_STATUS_LOGIN_SUCCESS)
        info = ResourcePacksInfoPacket()

        return status.encode() + info.encode()

    def _handle_resource_pack_client_response(
        self, packet: ResourcePackClientResponsePacket
    ) -> Optional[bytes]:
        if packet.status == RESOURCE_PACK_STATUS_COMPLETED:
            return self._send_start_game()
        if packet.status == RESOURCE_PACK_STATUS_HAVE_ALL_PACKS:
            stack = ResourcePackStackPacket()
            return stack.encode()
        return self._disconnect("disconnectionScreen.resourcePack")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _send_start_game(self) -> bytes:
        start_game = StartGamePacket(self._session_manager, self._session_info)
        transfer = TransferPacket(
            self._session_info.get_ip(), self._session_info.get_port()
        )
        return start_game.encode() + transfer.encode()

    def _disconnect(self, message: Optional[str] = None) -> bytes:
        packet = DisconnectPacket(
            hide_disconnect_screen=message is None,
            message=message or "",
        )
        return packet.encode()

    def identity_data(self) -> Optional[dict]:
        return self._identity_data
