"""Franchise signaling websocket client for NetherNet.

Ported from kastle's NetherNetXboxRpcSignaling / AbstractNetherNetXboxSignaling
(see PORTING_NOTES.md section 2). Runs on the shared asyncio loop.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Awaitable, Callable, Optional

import websockets

from ..logger import Logger  # noqa: F401  (kept for type references)

SIGNALING_URL = "wss://signal.franchise.minecraft-services.net/ws/v1.0/messaging/connect"
SIGNALING_USER_AGENT = "libHttpClient/1.0.0.0"

METHOD_TURN_AUTH = "Signaling_TurnAuth_v1_0"
METHOD_SEND_MESSAGE = "Signaling_SendClientMessage_v1_0"
METHOD_RECEIVE_MESSAGE = "Signaling_ReceiveMessage_v1_0"
METHOD_PING = "System_Ping_v1_0"
METHOD_PONG = "System_Pong_v1_0"
INNER_METHOD_WEBRTC = "Signaling_WebRtc_v1_0"
INNER_METHOD_DELIVERY = "Signaling_DeliveryNotification_V1_0"

SIGNAL_OFFER = "CONNECTREQUEST"
SIGNAL_ANSWER = "CONNECTRESPONSE"
SIGNAL_CANDIDATE = "CANDIDATEADD"
SIGNAL_ERROR = "CONNECTERROR"


def _ws_connect_kwargs(headers: dict) -> dict:
    """websockets>=14 renamed extra_headers to additional_headers."""
    try:
        version = tuple(int(p) for p in websockets.__version__.split(".")[:2])
    except ValueError:
        version = (0, 0)
    key = "additional_headers" if version >= (14, 0) else "extra_headers"
    return {key: headers}


def parse_turn_servers(result: dict) -> list[dict]:
    """Parse the TurnAuthServers response into aiortc-compatible dicts."""
    servers = result.get("TurnAuthServers") or result.get("turnAuthServers") or []
    out = []
    for server in servers:
        urls = server.get("Urls") or server.get("urls")
        if not urls:
            continue
        entry = {"urls": list(urls)}
        username = server.get("Username") or server.get("username")
        password = (
            server.get("Password")
            or server.get("password")
            or server.get("Credential")
            or server.get("credential")
        )
        if username:
            entry["username"] = username
        if password:
            entry["credential"] = password
        out.append(entry)
    return out


class Signal:
    def __init__(self, signal_type: str, connection_id: int, data: str, network_id: str) -> None:
        self.type = signal_type
        self.connection_id = connection_id
        self.data = data
        self.network_id = network_id

    @classmethod
    def parse(cls, text: str, network_id: str) -> "Signal":
        parts = text.split(" ", 2)
        if len(parts) < 3:
            raise ValueError(f"Invalid signal: {text[:80]}")
        return cls(parts[0], int(parts[1]), parts[2], network_id)

    def __str__(self) -> str:
        return f"{self.type} {self.connection_id} {self.data}"


class FranchiseSignaling:
    """Websocket signaling client; connects with the MC session token and
    exchanges WebRTC signals between the broadcaster and Minecraft clients."""

    def __init__(
        self,
        local_network_id: str,
        mc_token_header_provider: Callable[[], str],
        on_new_connection: Callable[[int, str, str], None],
        on_signal: Callable[[Signal], None],
        logger,
    ) -> None:
        self.local_network_id = local_network_id
        self._mc_token_provider = mc_token_header_provider
        self._on_new_connection = on_new_connection
        self._on_signal = on_signal
        self.logger = logger

        self._ws = None
        self._task: Optional[asyncio.Task] = None
        self._pending: dict[str, asyncio.Future] = {}
        self.ice_servers: list[dict] = []
        self.connected = asyncio.Event()
        self._closed = False
        self._turn_ready = asyncio.Event()

    def is_active(self) -> bool:
        if self._closed:
            return False
        ws = self._ws
        if ws is None:
            return False
        try:
            closed = getattr(ws, "closed", None)
            if closed is not None:  # websockets < 14
                return not closed
            # websockets >= 14: no .closed attribute
            return ws.protocol.state.name == "OPEN"
        except Exception:
            return False

    def start(self) -> None:
        from ..asyncio_runner import shared_runner

        self._task = shared_runner().submit(self._run())

    async def wait_ready(self, timeout: float = 15.0) -> None:
        await asyncio.wait_for(self._turn_ready.wait(), timeout)

    async def _run(self) -> None:
        # Auto-reconnect loop: transient drops recover in place instead of
        # forcing a full session recreation
        while not self._closed:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                break
            except Exception as ex:
                if self._closed:
                    break
                self.logger.warn(f"Signaling websocket lost ({ex}), reconnecting...")
            if self._closed:
                break
            self._ws = None
            self.connected.clear()
            self._turn_ready.clear()
            try:
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                break

    async def _run_once(self) -> None:
        headers = {
            "Authorization": self._mc_token_provider(),
            "User-Agent": SIGNALING_USER_AGENT,
            "session-id": str(uuid.uuid4()),
            "request-id": str(uuid.uuid4()),
        }
        async with websockets.connect(
            SIGNALING_URL,
            open_timeout=15,
            ping_interval=None,
            ping_timeout=None,
            **_ws_connect_kwargs(headers),
        ) as ws:
            if self._closed:
                return
            self._ws = ws
            self.connected.set()
            self.logger.info("Franchise signaling websocket connected")

            self._fetch_turn_auth_task = asyncio.create_task(self._fetch_turn_auth())
            self._turn_ready.set()

            ping_task = asyncio.create_task(self._ping_loop())
            try:
                async for message in ws:
                    await self._handle_message(message)
            finally:
                ping_task.cancel()

    async def _fetch_turn_auth(self) -> None:
        try:
            result = await self._rpc(METHOD_TURN_AUTH, {})
        except Exception as ex:
            self.logger.error("Failed to fetch TURN credentials", ex)
            return
        self.ice_servers = parse_turn_servers(result if isinstance(result, dict) else {})
        self.logger.debug(f"Successfully parsed {len(self.ice_servers)} ICE servers.")

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(20)
            try:
                await asyncio.wait_for(self._rpc(METHOD_PING, {}), 10)
            except Exception as ex:
                self.logger.debug(f"Signaling ping failed: {ex}")

    async def _rpc(self, method: str, params: dict) -> Optional[dict]:
        ws = self._ws
        if ws is None:
            raise RuntimeError("Signaling websocket not connected")
        request_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[request_id] = future
        await ws.send(
            json.dumps(
                {
                    "params": params,
                    "jsonrpc": "2.0",
                    "method": method,
                    "id": request_id,
                }
            )
        )
        try:
            return await asyncio.wait_for(future, 30)
        finally:
            self._pending.pop(request_id, None)

    async def _handle_message(self, message) -> None:
        try:
            json_data = json.loads(message)
        except (ValueError, TypeError):
            self.logger.debug(f"Unparseable signaling frame: {str(message)[:200]}")
            return
        if not isinstance(json_data, dict):
            return

        if "result" in json_data or ("error" in json_data and "id" in json_data):
            await self._handle_response(json_data)
        elif "method" in json_data:
            await self._handle_request(json_data)

    async def _handle_response(self, json_data: dict) -> None:
        request_id = json_data.get("id")
        if request_id is None:
            return
        future = self._pending.get(str(request_id))
        if future is None or future.done():
            return
        error = json_data.get("error")
        if error:
            message = error.get("message", json.dumps(error)) if isinstance(error, dict) else str(error)
            future.set_exception(RuntimeError(message))
        else:
            result = json_data.get("result")
            future.set_result(result if result is not None else {})
            if self._turn_ready.is_set() is False:
                self._turn_ready.set()

    async def _handle_request(self, json_data: dict) -> None:
        method = json_data.get("method")
        request_id = json_data.get("id")
        if method == METHOD_RECEIVE_MESSAGE:
            if request_id is not None:
                await self._send_result(request_id, None)
            params = json_data.get("params")
            if isinstance(params, list):
                for entry in params:
                    await self._process_incoming(entry)
            elif isinstance(params, dict):
                await self._process_incoming(params)
        elif method in (METHOD_PONG, METHOD_PING):
            if request_id is not None:
                await self._send_result(request_id, None)

    async def _process_incoming(self, msg_obj: dict) -> None:
        from_name = msg_obj.get("From")
        raw_inner = msg_obj.get("Message")
        msg_id = msg_obj.get("Id") or str(uuid.uuid4())
        if from_name is None or raw_inner is None:
            return

        # Acknowledge delivery
        inner_ack = {
            "params": {"messageId": msg_id},
            "jsonrpc": "2.0",
            "method": INNER_METHOD_DELIVERY,
        }
        try:
            await self._rpc(
                METHOD_SEND_MESSAGE, self._send_params(from_name, json.dumps(inner_ack))
            )
        except Exception as ex:
            self.logger.debug(f"Failed to ack signaling message: {ex}")

        try:
            inner = json.loads(raw_inner)
        except (ValueError, TypeError):
            self.logger.debug(f"Failed to parse inner signaling message: {str(raw_inner)[:120]}")
            return
        if inner.get("method") == INNER_METHOD_WEBRTC:
            payload = (inner.get("params") or {}).get("message")
            if payload is None:
                return
            try:
                signal = Signal.parse(str(payload), str(from_name))
            except ValueError as ex:
                self.logger.debug(str(ex))
                return
            if signal.type == SIGNAL_OFFER:
                self._on_new_connection(signal.connection_id, signal.network_id, signal.data)
            else:
                self._on_signal(signal)

    @staticmethod
    def _send_params(to_player_id: str, message: str) -> dict:
        return {
            "toPlayerId": to_player_id,
            "messageId": str(uuid.uuid4()),
            "message": message,
        }

    def send_signal_to(self, target_network_id: str, signal: Signal) -> None:
        from ..asyncio_runner import shared_runner

        inner = {
            "params": {
                "netherNetId": self.local_network_id,
                "message": str(signal),
            },
            "jsonrpc": "2.0",
            "method": INNER_METHOD_WEBRTC,
        }
        params = self._send_params(target_network_id, json.dumps(inner))

        async def _send() -> None:
            await self._rpc(METHOD_SEND_MESSAGE, params)

        shared_runner().submit(_send())

    async def _send_result(self, request_id, result) -> None:
        ws = self._ws
        if ws is None:
            return
        await ws.send(
            json.dumps({"id": request_id, "result": result, "jsonrpc": "2.0"})
        )

    def close(self) -> None:
        self._closed = True
        from ..asyncio_runner import shared_runner

        async def _close() -> None:
            ws = self._ws
            self._ws = None
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
            if self._task is not None:
                self._task.cancel()

        try:
            shared_runner().run(_close(), timeout=5)
        except Exception:
            pass
