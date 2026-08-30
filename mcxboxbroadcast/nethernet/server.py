"""NetherNet server: accepts Minecraft clients signaled through the franchise
signaling service, negotiates WebRTC (aiortc), runs the minimal Bedrock login
flow and transfers the client to the real server.

Ported from kastle NetherNetServerChannel/NetherNetChannel plus the Java
BroadcasterChannelInitializer/RedirectPacketHandler (see PORTING_NOTES.md).
"""

from __future__ import annotations

import asyncio
import struct
from typing import Callable, Optional

try:
    from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
    from aiortc.rtcicetransport import RTCIceCandidate
except ImportError:  # pragma: no cover
    RTCPeerConnection = None


def parse_candidate_string(candidate_sdp: str):
    """Parse a C++/SDP format candidate string ("candidate:foundation component
    protocol priority ip port typ type ...") into an aiortc RTCIceCandidate."""
    parts = candidate_sdp.split()
    if parts and parts[0].startswith("candidate:"):
        parts[0] = parts[0][len("candidate:") :]
    if len(parts) < 8:
        return None
    try:
        candidate = RTCIceCandidate(
            foundation=parts[0],
            component=int(parts[1]),
            protocol=parts[2].lower(),
            priority=int(parts[3]),
            ip=parts[4],
            port=int(parts[5]),
            type=parts[7],
        )
        candidate.sdpMid = "0"
        candidate.sdpMLineIndex = 0
        return candidate
    except (ValueError, IndexError):
        return None

from ..logger import Logger
from . import bedrock_packets as bp
from .identity import ServerIdentity
from .signaling import (
    SIGNAL_ANSWER,
    SIGNAL_CANDIDATE,
    SIGNAL_ERROR,
    SIGNAL_OFFER,
    FranchiseSignaling,
    Signal,
)

MAX_SCTP_MESSAGE_SIZE = 10000
HANDSHAKE_TIMEOUT = 30.0


def extract_candidates(sdp: str) -> list[str]:
    out = []
    for line in sdp.splitlines():
        if line.startswith("a=candidate:"):
            out.append(line[2:].strip())
    return out


class RedirectSession:
    """The minimal Bedrock packet flow that transfers a client to the target."""

    def __init__(self, peer: "NetherNetPeer") -> None:
        self.peer = peer
        self.server = peer.server
        self.logger = peer.logger
        self.network_settings_requested = False
        self.identity_data: Optional[dict] = None
        self._closed = False

    def handle_packet(self, packet_id: int, body: bytes) -> None:
        if packet_id == bp.PACKET_REQUEST_NETWORK_SETTINGS:
            self._handle_request_network_settings(body)
        elif packet_id == bp.PACKET_LOGIN:
            self._handle_login(body)
        elif packet_id == bp.PACKET_CLIENT_CACHE_STATUS:
            pass
        elif packet_id == bp.PACKET_RESOURCE_PACK_CLIENT_RESPONSE:
            self._handle_resource_pack_response(body)
        else:
            self.logger.debug(f"Ignoring bedrock packet {packet_id}")

    def _handle_request_network_settings(self, body: bytes) -> None:
        client_protocol = bp.parse_request_network_settings(body)
        server_protocol = self.server.protocol_version

        # The client normally prevents you connecting to a server with a
        # different protocol number, but double check here
        if client_protocol != server_protocol:
            self.disconnect(
                "disconnectionScreen.outdatedServer"
                if client_protocol > server_protocol
                else "disconnectionScreen.outdatedClient"
            )
            return

        self.network_settings_requested = True
        self.peer.send_raw_packet(bp.network_settings(0))
        self.peer.compressed = True

    def _handle_login(self, body: bytes) -> None:
        if not self.network_settings_requested:
            self.peer.send_raw_packet(bp.play_status(bp.PLAY_STATUS_FAILED_CLIENT_OLD))
            self.disconnect(None)
            return

        self.peer.send_raw_packet(bp.play_status(bp.PLAY_STATUS_LOGIN_SUCCESS))
        self.peer.send_raw_packet(bp.resource_packs_info())

        try:
            protocol, auth_jwt, client_jwt = bp.parse_login(body)
            if protocol != self.server.protocol_version:
                raise ValueError(f"Login protocol {protocol} does not match")
            signed, extra_data = bp.parse_login_chain(auth_jwt, client_jwt)
            if not signed:
                raise ValueError("Chain is not signed")
            self.identity_data = extra_data
        except Exception as ex:
            self.logger.debug(f"Failed to validate login packet: {ex}")
            self.disconnect("disconnect.loginFailed")

    def _handle_resource_pack_response(self, body: bytes) -> None:
        status, _response_type = bp.parse_resource_pack_client_response(body)
        if status == bp.RPC_STATUS_COMPLETED:
            self._send_start_game()
        elif status == bp.RPC_STATUS_HAVE_ALL_PACKS:
            self.peer.send_raw_packet(bp.resource_pack_stack())
        else:
            self.disconnect("disconnectionScreen.resourcePack")

    def _send_start_game(self) -> None:
        info = self.server.session_info
        self.peer.send_raw_packet(
            bp.start_game(info.host_name, info.world_name, info.players, info.max_players)
        )
        self.peer.send_raw_packet(bp.transfer(info.ip, info.port))

        if self.identity_data is not None:
            display_name = self.identity_data.get("displayName", "")
            xuid = self.identity_data.get("XUID", "")
            self.server.logger.info(
                f"Transferred bedrock client {display_name} ({xuid}) to target server."
            )
            try:
                from datetime import datetime, timezone

                self.server.player_history().last_seen(
                    str(xuid), datetime.now(timezone.utc)
                )
            except IOError:
                pass
        self.close()

    def disconnect(self, message: Optional[str]) -> None:
        try:
            self.peer.send_raw_packet(bp.disconnect(message))
        except Exception:
            pass
        self.close()

    def close(self) -> None:
        self._closed = True
        self.peer.close()


class NetherNetPeer:
    def __init__(self, server: "NetherNetServer", connection_id: int, remote_network_id: str) -> None:
        self.server = server
        self.logger = server.logger
        self.connection_id = connection_id
        self.remote_network_id = remote_network_id

        self.compressed = False
        self.reliable_channel = None
        self.unreliable_channel = None
        self.session: Optional[RedirectSession] = None
        self.closed = False

        self._assembly = bytearray()
        self._expected_segments = -1

        ice_servers = server.ice_servers()
        configuration = None
        if RTCPeerConnection is not None:
            rtc_ice = [
                RTCIceServer(urls=s["urls"], username=s.get("username"), credential=s.get("credential"))
                for s in ice_servers
            ]
            configuration = RTCConfiguration(iceServers=rtc_ice)
            self.pc = RTCPeerConnection(configuration=configuration)
        else:  # pragma: no cover
            raise RuntimeError("aiortc is required for NetherNet support")

        self.pc.on("datachannel")(self._on_datachannel)

    # -- signaling ------------------------------------------------------
    async def accept(self, offer_sdp: str) -> None:
        try:
            await self.pc.setRemoteDescription(
                RTCSessionDescription(sdp=offer_sdp, type="offer")
            )
            answer = await self.pc.createAnswer()
            await self.pc.setLocalDescription(answer)
            augmented = self.server.identity.augment_answer(self.pc.localDescription.sdp)
            self.server.signaling.send_signal_to(
                self.remote_network_id,
                Signal(SIGNAL_ANSWER, self.connection_id, augmented, ""),
            )
            for candidate in extract_candidates(self.pc.localDescription.sdp):
                self.server.signaling.send_signal_to(
                    self.remote_network_id,
                    Signal(SIGNAL_CANDIDATE, self.connection_id, candidate, ""),
                )
        except Exception as ex:
            self.logger.error(
                f"Failed to accept NetherNet connection {self.connection_id}: {ex}", ex
            )
            self.close()

    async def add_remote_candidate(self, candidate_sdp: str) -> None:
        try:
            candidate = parse_candidate_string(candidate_sdp)
            if candidate is None:
                self.logger.debug(
                    f"Failed to parse ICE candidate for {self.connection_id}: "
                    f"{candidate_sdp[:80]}"
                )
                return
            await self.pc.addIceCandidate(candidate)
        except Exception as ex:
            self.logger.debug(
                f"Failed to apply ICE candidate for {self.connection_id} "
                f"(connection likely closed): {ex}"
            )

    # -- data channels ---------------------------------------------------
    def _on_datachannel(self, channel) -> None:
        self.logger.debug(f"Received Data Channel: {channel.label}")
        if channel.label == "ReliableDataChannel":
            self.reliable_channel = channel
        elif channel.label == "UnreliableDataChannel":
            self.unreliable_channel = channel

        @channel.on("message")
        def on_message(message):  # noqa: ANN001
            if isinstance(message, str):
                message = message.encode()
            self._handle_channel_message(message)

        if self.reliable_channel is not None and self.unreliable_channel is not None:
            self._start_session()

    def _start_session(self) -> None:
        self.session = RedirectSession(self)

    # -- framing ----------------------------------------------------------
    def _handle_channel_message(self, message: bytes) -> None:
        if self.closed:
            return
        if len(message) < 1:
            return
        segments = message[0]
        payload = message[1:]
        if self._expected_segments == -1:
            self._expected_segments = segments
        elif segments != self._expected_segments - 1:
            # Invalid promised segments; drop the assembly
            self.logger.debug("Invalid promised segments on data channel")
            self._assembly.clear()
            self._expected_segments = -1
            return
        else:
            self._expected_segments = segments
        self._assembly += payload
        if segments == 0:
            try:
                payload = bytes(self._assembly)
                self._assembly.clear()
                self._expected_segments = -1
                self._handle_payload(payload)
            except Exception as ex:
                self.logger.error(f"Error processing NetherNet payload: {ex}", ex)
                self.close()

    def _handle_payload(self, payload: bytes) -> None:
        frames = bp.decode_batch(payload)
        for frame in frames:
            packet_id, body = bp.decode_packet_body(frame)
            if self.session is not None:
                self.session.handle_packet(packet_id, body)

    def send_raw_packet(self, raw_packet_body: bytes) -> None:
        """Send one bedrock packet (with compression framing applied)."""
        if self.closed:
            return
        if self.compressed:
            frame = bp.compress_frame(raw_packet_body)
        else:
            frame = bytes([bp.COMPRESSION_NONE]) + raw_packet_body
        payload = bp.encode_batch([frame])
        self._send_payload(payload)

    def _send_payload(self, payload: bytes) -> None:
        channel = self.reliable_channel
        if channel is None:
            return
        max_payload = MAX_SCTP_MESSAGE_SIZE - 1
        segments = (len(payload) + max_payload - 1) // max_payload
        offset = 0
        for i in range(segments):
            remaining = segments - 1 - i
            chunk = payload[offset : offset + max_payload]
            offset += len(chunk)
            try:
                channel.send(bytes([remaining]) + chunk)
            except Exception as ex:
                self.logger.debug(f"Failed to send on data channel: {ex}")
                self.close()
                return

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        if self.closed:
            return
        self.closed = True

        async def _close() -> None:
            try:
                await self.pc.close()
            except Exception:
                pass

        from ..asyncio_runner import shared_runner

        try:
            shared_runner().submit(_close())
        except Exception:
            pass
        self.server._remove_peer(self.connection_id)


class NetherNetServer:
    def __init__(
        self,
        local_network_id: int,
        mc_token_header_provider: Callable[[], str],
        session_info,
        storage_manager,
        logger: Logger,
        protocol_version: int = 2169,
    ) -> None:
        self.logger = logger.prefixed("NetherNet")
        self.session_info = session_info
        self._storage_manager = storage_manager
        self.protocol_version = protocol_version
        self.identity = ServerIdentity("self")
        self.local_network_id = str(local_network_id)

        self.signaling = FranchiseSignaling(
            self.local_network_id,
            mc_token_header_provider,
            self._on_new_connection,
            self._on_signal,
            self.logger,
        )
        self._peers: dict[int, NetherNetPeer] = {}
        self._ice_servers: list[dict] = []

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if RTCPeerConnection is None:  # pragma: no cover
            raise RuntimeError("aiortc is not installed; NetherNet support is unavailable")
        self.signaling.start()
        from ..asyncio_runner import shared_runner

        shared_runner().run(self.signaling.wait_ready(), timeout=20)
        self.logger.info(f"NetherNet Broadcaster started on ID: {self.local_network_id}")

    def ice_servers(self) -> list[dict]:
        return self._ice_servers

    def player_history(self):
        return self._storage_manager.player_history()

    # -- signaling callbacks (called on the asyncio loop) --------------------
    def _on_new_connection(self, connection_id: int, remote_network_id: str, offer_sdp: str) -> None:
        if connection_id in self._peers:
            return
        self.logger.debug(
            f"Incoming NetherNet connection {connection_id} from {remote_network_id}"
        )
        peer = NetherNetPeer(self, connection_id, remote_network_id)
        self._peers[connection_id] = peer
        asyncio.get_running_loop().create_task(peer.accept(offer_sdp))

    def _on_signal(self, signal: Signal) -> None:
        peer = self._peers.get(signal.connection_id)
        if peer is None:
            self.logger.debug(
                f"No handler found for connection ID: {signal.connection_id} (Type: {signal.type})"
            )
            return
        if signal.type == SIGNAL_CANDIDATE:
            asyncio.get_running_loop().create_task(peer.add_remote_candidate(signal.data))
        elif signal.type == SIGNAL_ERROR:
            self.logger.debug(f"Received CONNECT_ERROR for {signal.connection_id}")
            peer.close()

    def _remove_peer(self, connection_id: int) -> None:
        self._peers.pop(connection_id, None)

    def is_active(self) -> bool:
        return self.signaling.is_active()

    def close(self) -> None:
        peers = list(self._peers.values())
        for peer in peers:
            peer.close()
        self._peers.clear()
        self.signaling.close()
