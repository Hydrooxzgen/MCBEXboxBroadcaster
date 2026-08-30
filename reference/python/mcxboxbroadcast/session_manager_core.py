"""Core session manager, mirroring the Java ``SessionManagerCore``.

Authenticates with Xbox Live, creates/updates the multiplayer session in the
session directory, manages the RTA websocket and NetherNet redirect server, and
keeps the user's presence active.
"""

from __future__ import annotations

import os
import time
from abc import abstractmethod
from typing import Optional

import requests

from mcxboxbroadcast.auth.auth_manager import AuthManager, AgeVerificationException
from mcxboxbroadcast.constants import (
    CREATE_HANDLE,
    MAX_FRIENDS,
    SERVICE_CONFIG_ID,
    TEMPLATE_NAME,
    USER_PRESENCE,
    gson_dumps,
    gson_loads,
)
from mcxboxbroadcast.exceptions import (
    SessionCreationException,
    SessionUpdateException,
)
from mcxboxbroadcast.friend_manager import FriendManager
from mcxboxbroadcast.gallery_manager import GalleryManager
from mcxboxbroadcast.logger import Logger
from mcxboxbroadcast.models.session import (
    CreateHandleRequest,
    CreateHandleResponse,
    SessionRef,
    SocialSummaryResponse,
)
from mcxboxbroadcast.notifications import NotificationManager
from mcxboxbroadcast.rta_websocket_client import RtaWebsocketClient
from mcxboxbroadcast.scheduled_executor import ScheduledExecutor
from mcxboxbroadcast.session_info import ExpandedSessionInfo
from mcxboxbroadcast.storage import StorageManager


class SessionManagerCore:
    def __init__(
        self,
        storage_manager: StorageManager,
        notification_manager: NotificationManager,
        logger: Logger,
    ) -> None:
        self._http_client = requests.Session()

        self._logger = logger
        self._core_logger = logger.prefixed("")
        self._storage_manager = storage_manager
        self._notification_manager = notification_manager

        self._auth_manager = AuthManager(notification_manager, storage_manager, logger)
        self._friend_manager = FriendManager(self._http_client, logger, self)
        self._gallery_manager = GalleryManager(self._http_client, logger, self)

        self._rta_websocket: Optional[RtaWebsocketClient] = None
        self._session_info: Optional[ExpandedSessionInfo] = None
        self._last_session_response: Optional[str] = None
        self._nether_net_port_range: Optional[tuple[int, int]] = None

        self._initialized = False

    # ------------------------------------------------------------------
    # Abstract API
    # ------------------------------------------------------------------
    @abstractmethod
    def scheduled_thread(self) -> ScheduledExecutor: ...

    @abstractmethod
    def get_session_id(self) -> str: ...

    @abstractmethod
    def update_session(self) -> None:
        """Push the session information to the session directory."""
        raise SessionUpdateException("Not implemented")

    @abstractmethod
    def handle_friendship(self) -> bool:
        """Handle the friendship of the current user if needed. Returns True if being handled."""
        return False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def friend_manager(self) -> FriendManager:
        return self._friend_manager

    def notification_manager(self) -> NotificationManager:
        return self._notification_manager

    def gallery_manager(self) -> GalleryManager:
        return self._gallery_manager

    def storage_manager(self) -> StorageManager:
        return self._storage_manager

    def logger(self) -> Logger:
        return self._logger

    def session_info(self) -> ExpandedSessionInfo:
        return self._session_info

    def last_session_response(self) -> Optional[str]:
        return self._last_session_response

    def get_auth_manager(self) -> AuthManager:
        return self._auth_manager

    def init(self) -> None:
        """Authenticate and create the Xbox LIVE session."""
        if self._initialized:
            raise SessionCreationException("Already initialized!")

        self._logger.info("Starting SessionManager...")

        # Make sure we are logged in and get info
        try:
            self.get_auth_manager().get_manager()
        except AgeVerificationException:
            self._logger.error(
                "Authentication failed due to the account requiring age verification. "
                "Please login to xbox.com and complete the age verification process, then try again."
            )
            self._logger.error(
                "You can skip it/opt out and continue using the tool, but some features may not work correctly."
            )
            self.shutdown()
            return

        friend_count = -1
        try:
            friend_count = len(self._friend_manager.get())
        except Exception:
            pass

        self._logger.info(
            f"Successfully authenticated as {self.get_gamertag()} ({self.get_xuid()}) "
            f"with {friend_count}/{MAX_FRIENDS} friends"
        )

        if self.handle_friendship():
            self._logger.info("Waiting for friendship to be processed...")
            time.sleep(5)  # TODO: do a real callback not just wait

        self._logger.info("Creating Xbox LIVE session...")

        # Create the session
        self._create_session()

        # Update the presence
        self.update_presence()

        # Let the user know we are done
        self._logger.info("Creation of Xbox LIVE session was successful!")

        self._auth_manager.set_on_device_token_refresh_callback(self._on_device_token_refresh)

        self._initialized = True

    def update_nonces(self) -> None:
        """Update the nonces in the session based on the current players. Nothing by default."""

    def shutdown(self) -> None:
        if self._rta_websocket is not None:
            self._rta_websocket.close()
            self._rta_websocket = None
        self._shutdown_nethernet()
        self._initialized = False

    def get_token_header(self) -> str:
        try:
            return self._auth_manager.get_token_header()
        except Exception as e:
            self._logger.error(f"Failed to get auth header: {e}")
            return ""

    def get_xuid(self) -> str:
        return self._auth_manager.get_xuid()

    def get_gamertag(self) -> str:
        return self._auth_manager.get_gamertag()

    def get_mc_token_header(self) -> str:
        try:
            return self._auth_manager.get_mc_token_header()
        except Exception as e:
            self._logger.error(f"Failed to get MC token header: {e}")
            return ""

    # ------------------------------------------------------------------
    # Session creation
    # ------------------------------------------------------------------
    def _create_session(self) -> None:
        token = self.get_token_header()
        if not token:
            raise SessionCreationException("Failed to get authorization headers")

        # We only need a websocket for the primary session manager
        if self._session_info is not None:
            # Update the current session XUID
            self._session_info.set_xuid(self.get_xuid())

            # Create the RTA websocket connection
            self._setup_rta_websocket()

            # Wait for and use the connection ID from the websocket
            try:
                connection_id = self._wait_for_connection_id()
                self._session_info.set_connection_id(connection_id)
            except Exception as e:
                raise SessionCreationException(f"Unable to get connectionId for session: {e}") from e

            self._setup_nethernet()

        # Set the showcase image to the current screenshot
        screenshot = self._storage_manager.screenshot()
        if screenshot and os.path.exists(screenshot):
            self._logger.info("Setting showcase image")
            if self._gallery_manager.set_showcase(screenshot):
                self._logger.info("Successfully set showcase image")

        # Push the session information to the session directory
        self.update_session()

        # Create the session handle
        content = CreateHandleRequest(
            version=1,
            type="activity",
            sessionRef=SessionRef(
                SERVICE_CONFIG_ID,
                TEMPLATE_NAME,
                self.get_session_id(),
            ),
        )

        try:
            response = self._http_client.post(
                CREATE_HANDLE,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": token,
                    "x-xbl-contract-version": "107",
                },
                data=gson_dumps(
                    {
                        "version": content.version,
                        "type": content.type,
                        "sessionRef": content.sessionRef.__dict__,
                    }
                ),
                timeout=30,
            )
            if self._session_info is not None:
                try:
                    parsed = gson_loads(response.text)
                    self._session_info.set_handle_id(parsed.get("id"))
                except Exception:
                    pass
        except requests.RequestException as e:
            raise SessionCreationException(str(e)) from e

        self._last_session_response = response.text

        if response.status_code not in (200, 201):
            self._logger.debug(
                f"Failed to create session handle '{self._last_session_response}' ({response.status_code})"
            )
            raise SessionCreationException(
                f"Unable to create session handle, got status {response.status_code} "
                f"trying to create: {response.text}"
            )

    def _on_device_token_refresh(self) -> None:
        try:
            self._logger.debug("Device token refreshed, recreating session...")
            self._create_session()
            self._logger.debug("Session recreated after device token refresh")
        except Exception as e:
            self._logger.error(f"Failed to recreate session after device token refresh: {e}")

    # ------------------------------------------------------------------
    # Session updates
    # ------------------------------------------------------------------
    def update_session_internal(self, url: str, data: object) -> str:
        """PUT the session data to ``url`` and return the response body."""
        try:
            response = self._http_client.put(
                url,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": self.get_token_header(),
                    "x-xbl-contract-version": "107",
                },
                data=gson_dumps(data),
                timeout=30,
            )
        except requests.RequestException as e:
            raise SessionUpdateException(str(e)) from e

        if response.status_code not in (200, 201):
            self._logger.info(f"Got update session response: {response.text}")
            raise SessionUpdateException(
                f"Unable to update session information, got status {response.status_code} "
                f"trying to update: {response.text}"
            )

        return response.text

    def check_connection(self) -> None:
        """Check the websocket connections and re-create the session if lost."""
        rta_is_open = self._rta_websocket is not None and self._rta_websocket.is_open()

        if not rta_is_open:
            try:
                self._logger.warn("Connection to websocket lost, re-creating session...")
                self._logger.debug(f"WebSocket status: RTA Open: {rta_is_open}")
                self._create_session()
                self._logger.info("WebSocket session reconnected")
            except (SessionCreationException, SessionUpdateException) as e:
                self._logger.error("Session is dead and hit exception trying to re-create it", exc_info=e)

    # ------------------------------------------------------------------
    # RTA websocket
    # ------------------------------------------------------------------
    def _setup_rta_websocket(self) -> None:
        if self._rta_websocket is not None:
            self._rta_websocket.close()
        self._rta_websocket = RtaWebsocketClient(self)
        self._rta_websocket.connect()

    def _wait_for_connection_id(self) -> str:
        return self._rta_websocket.wait_for_connection_id(timeout=10)

    # ------------------------------------------------------------------
    # NetherNet
    # ------------------------------------------------------------------
    def set_nether_net_port_range(self, min_port: int, max_port: int) -> None:
        """Restrict the local UDP port range used for WebRTC (NetherNet) ICE candidates.

        Passing 0 for both keeps the transport default (OS ephemeral range).
        """
        if min_port <= 0 and max_port <= 0:
            self._nether_net_port_range = None
            return
        self._nether_net_port_range = (min_port, max_port)

    def _setup_nethernet(self) -> None:
        """Start the NetherNet redirect server.

        See the ``nethernet`` module: it registers the NetherNet ID for the
        session and, when ``aiortc`` is available, accepts WebRTC connections
        from Minecraft clients, performs the Bedrock handshake and transfers
        them to the real server.
        """
        from mcxboxbroadcast.nethernet import NetherNetServer

        self._shutdown_nethernet()

        nether_net_id = int(self._session_info.get_nether_net_id())
        self._session_info.set_pmsg_id(self._auth_manager.get_mc_token_pmid())

        self._nethernet_server = NetherNetServer(
            nether_net_id=nether_net_id,
            session_manager=self,
            logger=self._logger,
            ice_port_range=getattr(self, "_nether_net_port_range", None),
        )
        started = self._nethernet_server.start()
        if not started:
            # aiortc missing: the session still shows in the friends list, but
            # players cannot join through NetherNet. Mirror the Java behaviour
            # by failing hard so the standalone bootstrap can report it.
            raise SessionCreationException("Unable to start NetherNet channel")

    def _shutdown_nethernet(self) -> None:
        server = getattr(self, "_nethernet_server", None)
        if server is not None:
            server.stop()
            self._nethernet_server = None

    # ------------------------------------------------------------------
    # Presence
    # ------------------------------------------------------------------
    def update_presence(self) -> None:
        """Update the presence of the current user on Xbox LIVE."""
        try:
            response = self._http_client.post(
                USER_PRESENCE % self.get_xuid(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": self.get_token_header(),
                    "x-xbl-contract-version": "3",
                },
                data='{"state": "active"}',
                timeout=30,
            )
        except requests.RequestException as e:
            self._logger.error(f"Failed to update presence: {e}")
            return

        heartbeat_after = 300
        if response.status_code != 200:
            self._logger.error(f"Failed to update presence, got status {response.status_code}")
        else:
            try:
                heartbeat_after = int(response.headers.get("X-Heartbeat-After", "300"))
            except ValueError:
                self._logger.debug("Failed to parse heartbeat after header, using default of 300")

        self._logger.debug(
            f"Presence update successful, scheduling presence update in {heartbeat_after} seconds"
        )
        self.scheduled_thread().schedule(self.update_presence, heartbeat_after)

    # ------------------------------------------------------------------
    # Social summary
    # ------------------------------------------------------------------
    def social_summary(self) -> SocialSummaryResponse:
        try:
            response = self._http_client.get(
                "https://social.xboxlive.com/users/me/summary",
                headers={"Authorization": self.get_token_header()},
                timeout=30,
            )
            data = gson_loads(response.text)
            return SocialSummaryResponse(
                targetFollowingCount=data.get("targetFollowingCount", -1),
                targetFollowerCount=data.get("targetFollowerCount", -1),
                isCallerFollowingTarget=data.get("isCallerFollowingTarget", False),
                isTargetFollowingCaller=data.get("isTargetFollowingCaller", False),
                hasCallerMarkedTargetAsFavorite=data.get("hasCallerMarkedTargetAsFavorite", False),
                hasCallerMarkedTargetAsKnown=data.get("hasCallerMarkedTargetAsKnown", False),
                legacyFriendStatus=data.get("legacyFriendStatus", ""),
                availablePeopleSlots=data.get("availablePeopleSlots", -1),
                recentChangeCount=data.get("recentChangeCount", -1),
                watermark=data.get("watermark", ""),
            )
        except Exception as e:
            self._logger.error(f"Unable to get current friend count: {e}")
            return SocialSummaryResponse()
