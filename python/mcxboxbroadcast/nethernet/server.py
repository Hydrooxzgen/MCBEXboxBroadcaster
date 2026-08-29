"""NetherNet redirect server, mirroring the kastle-based NetherNet setup.

The Java implementation uses the kastle library to expose a WebRTC (NetherNet)
transport registered against Xbox Live, running the Bedrock protocol over the
resulting data channel.

In Python this maps to ``aiortc``: a single ``RTCPeerConnection`` is created per
incoming Minecraft client (registered through the Xbox signaling backend), and
the Bedrock data channel frames are fed through :class:`RedirectPacketHandler`.

``aiortc`` is an optional dependency; without it the server logs a warning and
the session still works for presence/friends purposes, just without joinable
NetherNet transport.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Optional

from mcxboxbroadcast.constants import CONNECTION_TYPE_JSON_RPC
from mcxboxbroadcast.logger import Logger
from mcxboxbroadcast.nethernet.handler import RedirectPacketHandler
from mcxboxbroadcast.nethernet.packets import read_varint, write_varint

try:
    from aiortc import RTCPeerConnection, RTCDataChannel, RTCSessionDescription  # type: ignore
    from aiortc.contrib.signaling import TcpSocketSignaling  # type: ignore

    AIORTC_AVAILABLE = True
except ImportError:  # pragma: no cover
    AIORTC_AVAILABLE = False
    RTCPeerConnection = None  # type: ignore
    RTCDataChannel = None  # type: ignore
    RTCSessionDescription = None  # type: ignore


class NetherNetServer:
    """WebRTC server that redirects Bedrock clients to the real server.

    :param nether_net_id: The NetherNet ID advertised in the session, derived
        from :meth:`ExpandedSessionInfo.get_nether_net_id`.
    :param session_manager: The owning session manager (core).
    :param logger: The shared logger (used with a "NetherNet" prefix).
    :param ice_port_range: Optional ``(min, max)`` UDP port range for ICE
        candidates, mirroring ``setNetherNetPortRange``.
    """

    def __init__(
        self,
        nether_net_id: int,
        session_manager: "object",
        logger: Logger,
        ice_port_range: Optional[tuple[int, int]] = None,
    ) -> None:
        self._nether_net_id = nether_net_id
        self._session_manager = session_manager
        self._logger = logger.prefixed("NetherNet")
        self._ice_port_range = ice_port_range

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._peer_connections: list = []
        self._stopping = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> bool:
        """Start the server thread. Returns False when the transport is unavailable."""
        if not AIORTC_AVAILABLE:
            self._logger.warn(
                "aiortc is not installed - NetherNet transport is disabled. "
                "Install it with `pip install aiortc` to allow players to join "
                "through the redirect server."
            )
            return False

        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="NetherNet", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stopping.set()
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._stop_loop)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        finally:
            self._loop.close()
            self._loop = None

    def _stop_loop(self) -> None:
        async def _stop() -> None:
            for pc in list(self._peer_connections):
                await pc.close()
            self._peer_connections.clear()
            self._loop.stop()

        asyncio.ensure_future(_stop(), loop=self._loop)

    async def _serve(self) -> None:
        """Main loop: accept one peer connection at a time over the signaling channel."""
        if not AIORTC_AVAILABLE:
            return

        from mcxboxbroadcast.nethernet.signaling import XboxRpcSignalingBackend

        signaling = XboxRpcSignalingBackend(
            nether_net_id=self._nether_net_id,
            session_manager=self._session_manager,
            logger=self._logger,
        )

        self._logger.info(
            f"NetherNet Broadcaster started on ID: {self._nether_net_id}"
            + (
                f" (ICE ports {self._ice_port_range[0]}-{self._ice_port_range[1]})"
                if self._ice_port_range
                else ""
            )
        )

        while not self._stopping.is_set():
            try:
                await signaling.wait_for_client()
                pc = RTCPeerConnection()
                self._peer_connections.append(pc)

                @pc.on("datachannel")
                def _on_datachannel(channel: RTCDataChannel) -> None:
                    self._logger.debug(f"Data channel opened: {channel.label}")
                    self._handle_channel(channel)

                await signaling.configure(pc)
                await signaling.handshake(pc)

                # Keep the connection alive until it closes
                while not self._stopping.is_set() and pc.connectionState not in (
                    "failed",
                    "closed",
                    "disconnected",
                ):
                    await asyncio.sleep(1)

                if pc in self._peer_connections:
                    self._peer_connections.remove(pc)
                await pc.close()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._logger.error(f"NetherNet connection error: {e}")
                await asyncio.sleep(1)

    # ------------------------------------------------------------------
    # Bedrock data channel handling
    # ------------------------------------------------------------------
    def _handle_channel(self, channel: RTCDataChannel) -> None:
        session_info = self._session_manager.session_info()
        handler = RedirectPacketHandler(self._session_manager, session_info, self._logger)

        buffer = bytearray()

        def _on_message(message: object) -> None:
            nonlocal buffer
            if isinstance(message, bytes):
                buffer.extend(message)
            elif isinstance(message, str):
                buffer.extend(message.encode("utf-8"))
            else:
                return

            # Feed complete frames (varint length + payload) to the handler
            offset = 0
            while offset < len(buffer):
                try:
                    length, new_offset = read_varint(bytes(buffer), offset)
                except (ValueError, IndexError):
                    break
                end = new_offset + length
                if end > len(buffer):
                    break
                frame = bytes(buffer[new_offset:end])
                del buffer[:end]

                response = handler.handle_frame(frame)
                if response is not None:
                    try:
                        channel.send(write_varint(len(response)) + response)
                    except Exception as e:
                        self._logger.debug(f"Failed to send response frame: {e}")

        channel.on("message")(_on_message)


__all__ = ["NetherNetServer", "AIORTC_AVAILABLE"]
