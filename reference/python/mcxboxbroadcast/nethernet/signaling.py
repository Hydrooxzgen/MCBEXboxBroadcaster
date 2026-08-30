"""Xbox Live RPC signaling for NetherNet, mirroring kastle's
``NetherNetXboxRpcSignaling``.

NetherNet signaling runs over the Xbox Live JSON-RPC endpoint. The Minecraft
client discovers the broadcaster through the session's NetherNet ID and
exchanges WebRTC SDP offers/answers and ICE candidates over this channel.

The exact framing here follows the publicly documented behaviour of kastle and
the amphibian project; the endpoint and method names may need tweaking against
live Xbox servers.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

import requests

from mcxboxbroadcast.logger import Logger

# Xbox Live JSON-RPC endpoint used for NetherNet signaling
RPC_ENDPOINT = "https://rpc-prod.xboxlive.com"

# Signaling message types
MSG_TYPE_UNSPECIFIED = 0
MSG_TYPE_OFFER = 1
MSG_TYPE_ANSWER = 2
MSG_TYPE_ICE = 3

POLL_INTERVAL = 1.0
POLL_TIMEOUT = 45.0


class XboxRpcSignalingBackend:
    """Server-side signaling backend.

    Polls the Xbox RPC endpoint for incoming offers from Minecraft clients and
    feeds SDP/ICE messages into the given ``RTCPeerConnection``.
    """

    def __init__(
        self,
        nether_net_id: int,
        session_manager: "object",
        logger: Logger,
    ) -> None:
        self._nether_net_id = nether_net_id
        self._session_manager = session_manager
        self._logger = logger.prefixed("NetherNet Signaling")

        self._http = requests.Session()
        self._pending: list[dict] = []
        self._poll_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # RPC helpers
    # ------------------------------------------------------------------
    def _rpc(self, method: str, params: dict) -> Optional[dict]:
        headers = {
            "Content-Type": "application/json",
            "Authorization": self._session_manager.get_mc_token_header(),
            "Accept": "application/json",
        }
        body = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": str(int(time.time() * 1000)),
        }
        try:
            response = self._http.post(
                RPC_ENDPOINT, headers=headers, data=json.dumps(body), timeout=15
            )
            if response.status_code != 200:
                self._logger.debug(
                    f"RPC {method} returned {response.status_code}: {response.text[:200]}"
                )
                return None
            payload = response.json()
            return payload.get("result") or payload
        except (requests.RequestException, ValueError) as e:
            self._logger.debug(f"RPC {method} failed: {e}")
            return None

    def _register(self) -> None:
        self._rpc(
            "NetherNet.setMessageHandler",
            {
                "sessionId": self._nether_net_id,
                "username": self._session_manager.get_xuid(),
            },
        )

    def _get_messages(self) -> list:
        result = self._rpc(
            "NetherNet.getMessages",
            {
                "sessionId": self._nether_net_id,
                "username": self._session_manager.get_xuid(),
            },
        )
        if not result:
            return []
        return result.get("messages") or []

    def _send_message(self, target_id: str, message_type: int, message: dict) -> None:
        self._rpc(
            "NetherNet.sendMessage",
            {
                "sessionId": self._nether_net_id,
                "username": self._session_manager.get_xuid(),
                "targetId": target_id,
                "messageType": message_type,
                "message": json.dumps(message),
            },
        )

    # ------------------------------------------------------------------
    # asyncio integration
    # ------------------------------------------------------------------
    async def wait_for_client(self) -> None:
        """Block until an offer from a new client has been queued."""
        self._register()
        while True:
            if self._pending:
                return
            await asyncio.sleep(POLL_INTERVAL)

    async def configure(self, pc: "object") -> None:
        """Start polling for messages and wire up the peer connection events."""

        async def _poll() -> None:
            started = time.monotonic()
            while True:
                try:
                    messages = await asyncio.to_thread(self._get_messages)
                    for message in messages:
                        self._pending.append(message)
                except Exception as e:
                    self._logger.debug(f"Polling error: {e}")
                if time.monotonic() - started > POLL_TIMEOUT:
                    break
                await asyncio.sleep(POLL_INTERVAL)

        self._poll_task = asyncio.create_task(_poll())

        @pc.on("icecandidate")
        async def _on_ice(candidate: "object") -> None:
            if candidate is None:
                return
            # Push our ICE candidate back to the client
            self._send_message(
                self._current_client,
                MSG_TYPE_ICE,
                {"candidate": candidate.sdp},
            )

    async def handshake(self, pc: "object") -> None:
        """Consume queued signaling messages and complete the WebRTC handshake."""
        # Take the first pending offer as the current client
        while self._pending:
            message = self._pending.pop(0)
            msg_type = message.get("messageType", MSG_TYPE_UNSPECIFIED)
            payload = json.loads(message.get("message", "{}"))
            target_id = message.get("username") or message.get("senderId") or ""
            self._current_client = target_id

            if msg_type == MSG_TYPE_OFFER or "sdp" in payload:
                from aiortc import RTCSessionDescription

                await pc.setRemoteDescription(
                    RTCSessionDescription(
                        sdp=payload.get("sdp") or payload.get("description", {}).get("sdp", ""),
                        type=payload.get("type", "offer"),
                    )
                )
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                self._send_message(
                    target_id,
                    MSG_TYPE_ANSWER,
                    {"type": "answer", "sdp": pc.localDescription.sdp},
                )
                break
            elif msg_type == MSG_TYPE_ICE:
                await pc.addIceCandidate({"sdpMid": "0", "sdpMLineIndex": 0, "candidate": payload.get("candidate", "")})
            else:
                self._logger.debug(f"Unhandled signaling message type: {msg_type}")
