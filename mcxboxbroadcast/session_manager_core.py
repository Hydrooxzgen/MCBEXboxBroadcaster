"""Session manager core, ported from Java SessionManagerCore.java."""

from __future__ import annotations

import threading
from typing import Optional

import requests

from . import constants, http
from .auth.auth_manager import AuthManager
from .exceptions import AgeVerificationException, SessionCreationException, SessionUpdateException
from .friend_manager import FriendManager
from .gallery_manager import GalleryManager
from .logger import Logger
from .models.session import CreateHandleRequest, CreateHandleResponse, SessionRef, SocialSummaryResponse
from .notifications.notification_manager import NotificationManager
from .rta_websocket_client import RtaWebsocketClient
from .storage.storage_manager import StorageManager


class SessionManagerCore:
    def __init__(
        self,
        storage_manager: StorageManager,
        notification_manager: NotificationManager,
        logger: Logger,
    ) -> None:
        self.httpClient = requests.Session()
        self.logger = logger
        self.core_logger = logger

        self.storage_manager_: StorageManager = storage_manager
        self.notification_manager_: NotificationManager = notification_manager

        self.auth_manager: AuthManager = AuthManager(
            notification_manager, storage_manager, logger
        )
        self.friend_manager_: FriendManager = FriendManager(
            self.httpClient, logger, self
        )
        self.gallery_manager_: GalleryManager = GalleryManager(
            self.httpClient, logger, self
        )

        self.rtaWebsocket: Optional[RtaWebsocketClient] = None
        self.session_info = None  # Optional[ExpandedSessionInfo]
        self.last_session_response: Optional[str] = None
        self.initialized = False

        self.nether_net_server = None
        self._nether_net_port_range: Optional[tuple[int, int]] = None
        self._create_session_lock = threading.RLock()

    # ------------------------------------------------------------- accessors
    def friendManager(self) -> FriendManager:
        return self.friend_manager_

    def notification_manager(self) -> NotificationManager:
        return self.notification_manager_

    def galleryManager(self) -> GalleryManager:
        return self.gallery_manager_

    def storage_manager(self) -> StorageManager:
        return self.storage_manager_

    def logger_(self) -> Logger:
        return self.logger

    # ------------------------------------------------------------- abstract
    def scheduled_thread(self):
        raise NotImplementedError

    def get_session_id(self) -> str:
        raise NotImplementedError

    def handle_friendship(self) -> bool:
        raise NotImplementedError

    def update_session(self) -> None:
        raise NotImplementedError

    # ----------------------------------------------------------------- init
    def init(self) -> None:
        if self.initialized:
            raise SessionCreationException("Already initialized!")

        self.logger.verbose("Starting SessionManager...")

        # Make sure we are logged in and get info
        try:
            self.get_auth_manager()
        except AgeVerificationException:
            self.logger.error(
                "Authentication failed due to the account requiring age verification. "
                "Please login to xbox.com and complete the age verification process, "
                "then try again."
            )
            self.logger.error(
                "You can skip it/opt out and continue using the tool, but some "
                "features may not work correctly."
            )
            self.shutdown()
            return

        friend_count = -1
        try:
            friend_count = len(self.friend_manager_.get())
        except Exception:
            pass

        self.logger.info(
            f"Successfully authenticated as {self.get_gamertag()} ({self.get_xuid()}) "
            f"with {friend_count}/{constants.MAX_FRIENDS} friends"
        )

        if self.handle_friendship():
            self.logger.info("Waiting for friendship to be processed...")
            import time

            try:
                time.sleep(5)
            except KeyboardInterrupt:
                self.logger.error("Failed to wait for friendship to be processed")

        self.logger.info("Creating Xbox LIVE session...")
        try:
            self._create_session()
            self._update_presence()
            self.logger.info("Creation of Xbox LIVE session was successful!")
        except (SessionCreationException, SessionUpdateException):
            raise

        self.auth_manager.set_on_device_token_refresh_callback(self._on_device_token_refresh)

        self.initialized = True

    def _on_device_token_refresh(self) -> None:
        try:
            self.logger.debug("Device token refreshed, recreating session...")
            self._create_session()
            self.logger.debug("Session recreated after device token refresh")
        except Exception as ex:
            self.logger.error("Failed to recreate session after device token refresh", ex)

    # -------------------------------------------------------- session create
    def _create_session(self) -> None:
        with self._create_session_lock:
            try:
                token = self.get_auth_manager().get_authorization_header()
            except Exception as ex:
                raise SessionCreationException(f"Failed to get authorization headers: {ex}")

            # We only need a websocket for the primary session manager
            if self.session_info is not None:
                self.session_info.xuid = self.get_xuid()

                self._setup_rta_websocket()

                try:
                    connection_id = self._wait_for_connection_id()
                    self.session_info.connection_id = connection_id
                except Exception as ex:
                    raise SessionCreationException(f"Unable to get connectionId for session: {ex}")

                self._setup_nether_net()

                if self.nether_net_server is None or not self.nether_net_server.is_active():
                    raise SessionCreationException("Unable to start NetherNet channel")

            # Set the showcase image to the current screenshot
            image_data = self.storage_manager_.screenshot()
            if image_data is not None:
                self.logger.info("Setting showcase image")
                if self.gallery_manager_.set_showcase(image_data, self.storage_manager_.screenshot_last_modified()):
                    self.logger.info("Successfully set showcase image")

            # Push the session information to the session directory
            self.update_session()

            # Create the session handle request
            content = CreateHandleRequest(
                1,
                "activity",
                SessionRef(constants.SERVICE_CONFIG_ID, constants.TEMPLATE_NAME, self.get_session_id()),
            )

            response = http.post_json(
                self.httpClient,
                constants.CREATE_HANDLE,
                {
                    "Authorization": token,
                    "x-xbl-contract-version": "107",
                },
                content.to_json(),
            )

            if self.session_info is not None:
                try:
                    parsed = CreateHandleResponse.from_json(response.json())
                    self.session_info.handle_id = parsed.id
                except ValueError:
                    pass

            self.last_session_response = response.text

            if response.status_code not in (200, 201):
                self.logger.debug(
                    f"Failed to create session handle '{self.last_session_response}' "
                    f"({response.status_code})"
                )
                raise SessionCreationException(
                    f"Unable to create session handle, got status {response.status_code} "
                    f"trying to create: {response.text}"
                )

    def update_session_internal(self, url: str, data) -> str:
        import json as _json

        response = self.httpClient.put(
            url,
            headers={
                "Content-Type": "application/json",
                "Authorization": self.get_token_header(),
                "x-xbl-contract-version": "107",
            },
            data=_json.dumps(data.to_json() if hasattr(data, "to_json") else data),
            timeout=http.DEFAULT_TIMEOUT,
        )

        if response.status_code not in (200, 201):
            self.logger.info(f"Got update session response: {response.text}")
            raise SessionUpdateException(
                f"Unable to update session information, got status {response.status_code} "
                f"trying to update: {response.text}"
            )
        return response.text

    def check_connection(self) -> None:
        rta_is_open = self.rtaWebsocket is not None and self.rtaWebsocket.is_open()
        rtc_is_open = self.nether_net_server is not None and self.nether_net_server.is_active()

        if not rta_is_open or not rtc_is_open:
            try:
                self.logger.warn("Connection to websocket lost, re-creating session...")
                self.logger.debug(
                    f"WebSocket status: RTA Open: {rta_is_open} RTC Open: {rtc_is_open}"
                )
                self._create_session()
                self.logger.info("WebSocket session reconnected")
            except (SessionCreationException, SessionUpdateException) as ex:
                self.logger.error("Session is dead and hit exception trying to re-create it", ex)

    def get_token_header(self) -> str:
        try:
            return self.get_auth_manager().get_authorization_header()
        except Exception as ex:
            self.logger.error("Failed to get auth header", ex)
            return ""

    def get_mc_token_header(self) -> Optional[str]:
        try:
            return self.get_auth_manager().get_mc_token_header()
        except Exception as ex:
            self.logger.error("Failed to get MC token header", ex)
            return None

    def _wait_for_connection_id(self) -> str:
        return self.rtaWebsocket.connection_id_future.result(
            timeout=constants.WEBSOCKET_CONNECTION_TIMEOUT
        )

    def _setup_rta_websocket(self) -> None:
        if self.rtaWebsocket is not None:
            self.rtaWebsocket.close()
        core = self

        def on_friend_request() -> None:
            core.scheduled_thread().submit(core.friend_manager_.accept_pending_friend_requests)

        def on_nonce_update() -> None:
            def run() -> None:
                try:
                    core.update_nonces()
                except SessionUpdateException as ex:
                    core.logger.error(
                        f"RTA Websocket [{core.rtaWebsocket.connection_id if core.rtaWebsocket else ''}] "
                        f"failed to update session nonces: {ex}",
                        ex,
                    )

            core.scheduled_thread().submit(run)

        self.rtaWebsocket = RtaWebsocketClient(
            lambda: core.get_token_header(),
            lambda: core.get_xuid(),
            on_friend_request,
            on_nonce_update,
            prefix=self.get_session_id() if self.session_info is not None else "",
        )
        self.rtaWebsocket.connect()

    def set_nether_net_port_range(self, min_port: int, max_port: int) -> None:
        if min_port <= 0 and max_port <= 0:
            self._nether_net_port_range = None
            return
        self._nether_net_port_range = (min_port, max_port)

    def nether_net_port_range(self) -> Optional[tuple[int, int]]:
        return self._nether_net_port_range

    def _setup_nether_net(self) -> None:
        self._shutdown_nether_net()

        from .asyncio_runner import shared_runner
        from .nethernet.server import NetherNetServer

        if self._nether_net_port_range:
            self.logger.warn(
                "The ICE port range option is not supported by the Python WebRTC "
                "stack and will be ignored."
            )

        core = self
        self.nether_net_server = NetherNetServer(
            self.session_info.nether_net_id,
            lambda: core.get_mc_token_header() or "",
            self.session_info,
            self.storage_manager_,
            self.logger,
            protocol_version=constants.PROTOCOL_VERSION,
        )
        self.session_info.pmsg_id = self.get_auth_manager().get_pmsg_id()
        if self.session_info.pmsg_id is None:
            self.logger.error(
                "pmsgId is missing from the Minecraft session token - "
                "clients will NOT be able to join the session!"
            )
        else:
            self.logger.verbose(
                f"Session connection info: NetherNetId={self.session_info.nether_net_id} "
                f"PmsgId={self.session_info.pmsg_id}"
            )
        self.nether_net_server.start()

    def shutdown(self) -> None:
        if self.rtaWebsocket is not None:
            self.rtaWebsocket.close()
        self._shutdown_nether_net()
        self.initialized = False

    def _shutdown_nether_net(self) -> None:
        if self.nether_net_server is not None:
            try:
                self.nether_net_server.close()
            except Exception as ex:
                self.logger.debug(f"Error closing NetherNet server: {ex}")
            self.nether_net_server = None

    # ------------------------------------------------------------- presence
    def _update_presence(self) -> None:
        response = self.httpClient.post(
            constants.USER_PRESENCE % self.get_xuid(),
            headers={
                "Content-Type": "application/json",
                "Authorization": self.get_token_header(),
                "x-xbl-contract-version": "3",
            },
            data='{"state": "active"}',
            timeout=http.DEFAULT_TIMEOUT,
        )

        heartbeat_after = 300
        if response.status_code != 200:
            self.logger.error(f"Failed to update presence, got status {response.status_code}")
        else:
            try:
                heartbeat_after = int(response.headers.get("X-Heartbeat-After", "300"))
            except ValueError:
                self.logger.debug("Failed to parse heartbeat after header, using default of 300")

        self.logger.debug(
            f"Presence update successful, scheduling presence update in {heartbeat_after} seconds"
        )
        self.scheduled_thread().schedule(self._update_presence, heartbeat_after)

    def social_summary(self) -> SocialSummaryResponse:
        try:
            response = http.get(
                self.httpClient,
                constants.SOCIAL_SUMMARY,
                {"Authorization": self.get_token_header()},
            )
            return SocialSummaryResponse.from_json(response.json())
        except (ValueError, requests.RequestException) as ex:
            self.logger.error("Unable to get current friend count", ex)
        return SocialSummaryResponse.empty()

    def get_xuid(self) -> str:
        return self.auth_manager.get_xuid()

    def get_gamertag(self) -> str:
        return self.auth_manager.get_gamertag()

    def get_auth_manager(self):
        return self.auth_manager.get_manager()

    def update_nonces(self) -> None:
        # Nothing by default
        pass
