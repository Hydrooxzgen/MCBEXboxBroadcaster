"""RTA (Real-Time Activity) websocket client, mirroring ``RtaWebsocketClient``.

Connects to ``wss://rta.xboxlive.com/connect``, subscribes for the connection ID
needed by the session directory, and forwards friend/session events.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Optional

import websockets

from mcxboxbroadcast.constants import RTA_WEBSOCKET, gson_loads
from mcxboxbroadcast.logger import Logger
from mcxboxbroadcast.models.ws import MessageType


class RtaWebsocketClient:
    def __init__(self, session_manager: "object") -> None:
        self._session_manager = session_manager
        self._connection_id: Optional[str] = None
        self._logger: Logger = session_manager.logger()
        self._xuid = session_manager.get_xuid()

        self._is_first_connection = True
        self._connection_id_event = threading.Event()
        self._connection_id_result: Optional[str] = None
        self._connection_id_error: Optional[BaseException] = None

        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._closed = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_connection_id(self) -> Optional[str]:
        return self._connection_id

    def is_open(self) -> bool:
        return self._ws is not None and not self._closed

    def connect(self) -> None:
        self.close()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name="RTA-Websocket", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._closed = True
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                loop = self._loop
                if loop is not None and loop.is_running():
                    asyncio.run_coroutine_threadsafe(ws.close(), loop).result(timeout=2.0)
                else:
                    asyncio.run(ws.close())
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def wait_for_connection_id(self, timeout: float = 10.0) -> str:
        """Block until the connection ID is received (or timeout)."""
        if not self._connection_id_event.wait(timeout):
            if self._connection_id_error is not None:
                raise self._connection_id_error
            raise TimeoutError("RTA websocket did not provide a connection id in time")
        if self._connection_id_error is not None:
            raise self._connection_id_error
        return self._connection_id_result

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _run(self) -> None:
        try:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._connect_loop())
        except Exception as e:
            self._logger.error(f"RTA Websocket [{self._connection_id}] error: {e}")
            self._complete_exceptionally(e)

    async def _connect_loop(self) -> None:
        while not self._closed:
            try:
                async with websockets.connect(
                    RTA_WEBSOCKET,
                    additional_headers={"Authorization": self._session_manager.get_token_header()},
                    max_size=None,
                ) as ws:
                    self._ws = ws
                    self._logger.debug(f"RTA Websocket [{self._connection_id}] connected")
                    await ws.send('[1,1,"https://sessiondirectory.xboxlive.com/connections/"]')
                    async for message in ws:
                        if self._closed:
                            break
                        self._on_message(message)
            except websockets.ConnectionClosed as e:
                if not self._closed:
                    self._logger.debug(
                        f"RTA Websocket [{self._connection_id}] disconnected: "
                        f"{e.reason or 'Normal close'} ({e.code})"
                    )
            except Exception as e:
                if not self._closed:
                    self._logger.error(f"RTA Websocket [{self._connection_id}] error: {e}")
            finally:
                self._ws = None
                self._complete_exceptionally(
                    Exception(f"RTA Websocket [{self._connection_id}] disconnected before connectionId was received")
                )
                if self._closed:
                    break
                # Wait a bit before reconnecting
                await asyncio.sleep(3)

    def _complete_exceptionally(self, error: BaseException) -> None:
        if not self._connection_id_event.is_set():
            self._connection_id_error = error
            self._connection_id_event.set()

    def _on_message(self, message: str) -> None:
        # [Type, SequenceId, ...]
        # Subscribe: [type, sequenceId, status, subscriptionId, data]
        # Unsubscribe: [type, sequenceId, status]
        # Event: [type, sequenceId, data]
        parts = gson_loads(message)
        type_value = parts[0]
        if isinstance(type_value, float):
            type_value = int(type_value)
        msg_type = MessageType.from_value(type_value)

        if msg_type is MessageType.Subscribe:
            self._logger.debug(f"RTA Websocket [{self._connection_id}] subscribed: {message}")
            if "ConnectionId" in message and self._is_first_connection:
                data = parts[4] if len(parts) > 4 else {}
                self._connection_id = data.get("ConnectionId")
                self._connection_id_result = self._connection_id
                self._connection_id_event.set()
                self._is_first_connection = False

                # Let Xbox know we want friend updates
                self._send_friend_subscribe()

        elif msg_type is MessageType.Unsubscribe:
            self._logger.debug(f"RTA Websocket [{self._connection_id}] unsubscribed: {message}")

        elif msg_type is MessageType.Event:
            self._logger.debug(f"RTA Websocket [{self._connection_id}] event: {message}")
            data = parts[2] if len(parts) > 2 else {}
            if not isinstance(data, dict):
                data = {}
            if data.get("NotificationType", "") == "IncomingFriendRequestCountChanged":
                self._logger.debug(f"RTA Websocket [{self._connection_id}] friend request: {message}")
                self._session_manager.friend_manager().accept_pending_friend_requests()

            # Check for a session update
            if "ncid" in data:
                try:
                    self._session_manager.update_nonces()
                except Exception as e:
                    self._logger.error(
                        f"RTA Websocket [{self._connection_id}] failed to update session nonces: {e}",
                        exc_info=e,
                    )

        elif msg_type is MessageType.Resync:
            self._logger.debug(f"RTA Websocket [{self._connection_id}] resync: {message}")

        else:
            self._logger.debug(f"RTA Websocket [{self._connection_id}] unknown: {message}")

    def _send_friend_subscribe(self) -> None:
        """Send the friend subscription message on the asyncio event loop."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(self._do_send_friend_subscribe(), loop)
            else:
                loop.run_until_complete(self._do_send_friend_subscribe())
        except Exception as e:
            self._logger.error(f"RTA Websocket [{self._connection_id}] failed to subscribe to friends: {e}")

    async def _do_send_friend_subscribe(self) -> None:
        if self._ws is not None:
            await self._ws.send(f'[1,2,"https://social.xboxlive.com/users/xuid({self._xuid})/friends"]')
