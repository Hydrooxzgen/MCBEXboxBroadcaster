"""RTA websocket client, ported from Java RtaWebsocketClient.java.

Runs on the shared asyncio loop; callbacks from the loop are dispatched back
onto the scheduled executor via the session manager.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
from typing import Callable, Optional

import websockets

from . import constants
from .logger import Logger

logger = Logger("RTA")


def _ws_connect_kwargs(headers: dict) -> dict:
    """websockets>=14 renamed extra_headers to additional_headers."""
    try:
        version = tuple(int(p) for p in websockets.__version__.split(".")[:2])
    except ValueError:
        version = (0, 0)
    key = "additional_headers" if version >= (14, 0) else "extra_headers"
    return {key: headers}


class RtaWebsocketClient:
    def __init__(
        self,
        token_header_provider: Callable[[], str],
        xuid_provider: Callable[[], str],
        on_friend_request: Callable[[], None],
        on_nonce_update: Callable[[], None],
        prefix: str = "",
    ) -> None:
        self._token_header_provider = token_header_provider
        self._xuid_provider = xuid_provider
        self.on_friend_request = on_friend_request
        self.on_nonce_update = on_nonce_update

        self.connection_id: Optional[str] = None
        self._connection_id_future: concurrent.futures.Future = concurrent.futures.Future()
        self._is_first_connection = True
        self._ws = None
        self._task: Optional[asyncio.Task] = None
        self._closed = False
        self._prefix = prefix

    @property
    def connection_id_future(self) -> concurrent.futures.Future:
        return self._connection_id_future

    def is_open(self) -> bool:
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

    def connect(self) -> None:
        from .asyncio_runner import shared_runner

        self._closed = False
        self._task = shared_runner().submit(self._run())

    def _fail_future_if_needed(self, reason: str) -> None:
        if not self._connection_id_future.done():
            self._connection_id_future.set_exception(
                Exception(f"RTA Websocket [{self.connection_id}] {reason}")
            )

    async def _run(self) -> None:
        try:
            headers = {"Authorization": self._token_header_provider()}
            async with websockets.connect(
                constants.RTA_WEBSOCKET,
                open_timeout=constants.WEBSOCKET_CONNECTION_TIMEOUT,
                **_ws_connect_kwargs(headers),
            ) as ws:
                if self._closed:
                    return
                self._ws = ws
                await ws.send('[1,1,"https://sessiondirectory.xboxlive.com/connections/"]')
                async for message in ws:
                    self._handle_message(message)
        except asyncio.CancelledError:
            pass
        except Exception as ex:
            if not self._closed:
                logger.error(f"RTA Websocket [{self._prefix}] error: {ex}", ex)
            self._fail_future_if_needed("error during connection")
        finally:
            self._ws = None
            self._fail_future_if_needed("disconnected before connectionId was received")

    def _handle_message(self, message) -> None:
        # [Type, SequenceId, ...]
        # Subscribe: [type, sequenceId, status, subscriptionId, data]
        # Unsubscribe: [type, sequenceId, status]
        # Event: [type, sequenceId, data]
        try:
            parts = json.loads(message)
        except (ValueError, TypeError):
            logger.debug(f"RTA Websocket [{self._prefix}] unparseable: {message}")
            return
        if not isinstance(parts, list) or not parts:
            return
        msg_type = parts[0]
        if msg_type == 1:  # Subscribe
            logger.verbose(f"RTA Websocket [{self._prefix}] connected and subscribed")
            if isinstance(parts, list) and len(parts) > 4 and isinstance(parts[4], dict):
                connection_id = parts[4].get("ConnectionId")
                if connection_id and self._is_first_connection:
                    self.connection_id = connection_id
                    if not self._connection_id_future.done():
                        self._connection_id_future.set_result(connection_id)
                    self._is_first_connection = False
                    # Let xbox know we want friend updates
                    xuid = self._xuid_provider()
                    asyncio.get_running_loop().create_task(
                        self._send(
                            f'[1,2,"https://social.xboxlive.com/users/xuid({xuid})/friends"]'
                        )
                    )
        elif msg_type == 2:  # Unsubscribe
            logger.debug(f"RTA Websocket [{self._prefix}] unsubscribed: {message}")
        elif msg_type == 3:  # Event
            logger.verbose(f"RTA Websocket [{self._prefix}] event received")
            data = parts[2] if len(parts) > 2 else {}
            if isinstance(data, dict):
                try:
                    if data.get("NotificationType") == "IncomingFriendRequestCountChanged":
                        logger.info(
                            f"RTA Websocket [{self._prefix}] friend request notification"
                        )
                        self.on_friend_request()
                except Exception as ex:
                    logger.error(
                        f"RTA Websocket [{self._prefix}] friend request handling failed: {ex}", ex
                    )
                try:
                    if "ncid" in data:
                        self.on_nonce_update()
                except Exception as ex:
                    logger.error(
                        f"RTA Websocket [{self._prefix}] failed to update session nonces: {ex}",
                        ex,
                    )
        elif msg_type == 4:  # Resync
            logger.debug(f"RTA Websocket [{self._prefix}] resync: {message}")
        else:
            logger.debug(f"RTA Websocket [{self._prefix}] unknown: {message}")

    async def _send(self, text: str) -> None:
        ws = self._ws
        if ws is not None:
            try:
                await ws.send(text)
            except Exception as ex:
                logger.debug(f"RTA Websocket [{self._prefix}] send failed: {ex}")

    def close(self) -> None:
        self._closed = True
        self._fail_future_if_needed("closed locally")

        from .asyncio_runner import shared_runner

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

        logger.debug(
            f"RTA Websocket [{self._prefix}] disconnected: Normal close (1000)"
        )
