"""Primary session manager, ported from Java SessionManager.java."""

from __future__ import annotations

import json
import secrets
from typing import Optional

import requests

from . import constants
from .exceptions import SessionCreationException, SessionUpdateException
from .logger import Logger
from .scheduled_executor import ScheduledExecutorService
from .session_info import ExpandedSessionInfo, SessionInfo
from .session_manager_core import SessionManagerCore
from .storage.storage_manager import StorageManager
from .notifications.notification_manager import NotificationManager
from .sub_session_manager import SubSessionManager


class SessionManager(SessionManagerCore):
    def __init__(
        self,
        storage_manager: StorageManager,
        notification_manager: NotificationManager,
        logger: Logger,
    ) -> None:
        super().__init__(storage_manager, notification_manager, logger.prefixed("Primary Session"))
        self.scheduledThreadPool = ScheduledExecutorService(5)
        self.sub_session_managers: dict[str, SubSessionManager] = {}
        self.nonces: dict[str, str] = {}

        self.friend_sync_config = None
        self.restart_callback = None
        self._session_info_holder: Optional[ExpandedSessionInfo] = None

    def scheduled_thread(self) -> ScheduledExecutorService:
        return self.scheduledThreadPool

    def get_session_id(self) -> str:
        return self.session_info.session_id

    @property
    def session_info(self):  # noqa: F811 - overrides the attribute with a typed holder
        return self._session_info_holder

    @session_info.setter
    def session_info(self, value) -> None:
        self._session_info_holder = value

    def init(self, session_info: SessionInfo, friend_sync_config) -> bool:
        # Set the internal session information based on the session info
        self.session_info = ExpandedSessionInfo("", "", session_info)

        super().init()

        # If we failed to initialize, don't continue with the rest of the setup
        if not self.initialized:
            return self.initialized

        # Set up the auto friend sync
        self.friend_sync_config = friend_sync_config
        self.friend_manager_.init(friend_sync_config)

        # Load sub-sessions from cache
        sub_sessions: list[str] = []
        try:
            sub_sessions_json = self.storage_manager_.sub_sessions()
            if sub_sessions_json.strip():
                sub_sessions = list(json.loads(sub_sessions_json))
        except (ValueError, IOError):
            pass

        # Create the sub-session managers in a worker so we don't block the main thread
        def create_sub_sessions() -> None:
            for sub_session in sub_sessions:
                try:
                    sub_session_manager = SubSessionManager(
                        sub_session,
                        self,
                        self.storage_manager_.sub_session(sub_session),
                        self.notification_manager_,
                        self.logger,
                    )
                    sub_session_manager.init()
                    sub_session_manager.friend_manager_.init(self.friend_sync_config)
                    self.sub_session_managers[sub_session] = sub_session_manager
                except (SessionCreationException, SessionUpdateException) as ex:
                    self.logger.error(f"Failed to create sub-session {sub_session}", ex)

        self.scheduledThreadPool.execute(create_sub_sessions)

        return self.initialized

    def handle_friendship(self) -> bool:
        # Don't do anything as we are the main session
        return False

    def update_session_with(self, session_info: SessionInfo) -> None:
        self.session_info.update_session_info(session_info)
        self.update_session()

    def update_nonces(self) -> None:
        import requests as _requests

        response = self.httpClient.get(
            constants.CREATE_SESSION % self.session_info.session_id,
            headers={
                "Content-Type": "application/json",
                "Authorization": self.get_token_header(),
                "x-xbl-contract-version": "107",
            },
            timeout=5,
        )
        try:
            session_response = CreateSessionResponse.from_json(response.json())
        except ValueError:
            raise SessionUpdateException(
                "Failed to get session for nonces, joining will not work: response is not json"
            )

        has_changes = False

        # Collect active XUIDs from the session
        active_xuids: set[str] = set()
        for member in session_response.members.values():
            system = (member.constants or {}).get("system") or {}
            xuid = system.get("xuid") if isinstance(system, dict) else getattr(system, "xuid", "")
            if xuid:
                active_xuids.add(str(xuid))

        # Remove our own xuid
        active_xuids.discard(self.session_info.xuid)

        # Remove stale nonces
        stale = set(self.nonces.keys()) - active_xuids
        if stale:
            for xuid in stale:
                del self.nonces[xuid]
            has_changes = True

        for xuid in active_xuids:
            if xuid not in self.nonces:
                # Generate a nonce
                nonce = secrets.token_hex(8)
                self.nonces[xuid] = nonce
                self.logger.debug(f"Generated nonce for XUID {xuid}: {nonce}")
                has_changes = True

        # Only update the session properties if something changed
        if has_changes:
            self.update_session()

    def update_session(self) -> None:
        # Make sure the websocket connection is still active
        self.check_connection()

        from .models.session import CreateSessionRequest

        response_body = self.update_session_internal(
            constants.CREATE_SESSION % self.session_info.session_id,
            CreateSessionRequest(self.session_info, self.nonces),
        )
        try:
            session_response = CreateSessionResponse.from_json(json.loads(response_body))

            # Restart if we have 28/30 session members
            players = len(session_response.members)
            if players >= 28:
                self.logger.info(f"Restarting session due to {players}/30 players")
                self.restart()
        except (ValueError, KeyError) as ex:
            raise SessionUpdateException(f"Failed to parse session response: {ex}")

    def shutdown(self) -> None:
        # Shutdown all sub-sessions
        for sub_session_manager in list(self.sub_session_managers.values()):
            sub_session_manager.shutdown()

        # Shutdown self
        super().shutdown()
        self.scheduledThreadPool.shutdown_now()

    def dump_session(self) -> None:
        try:
            self.storage_manager_.last_session_response(self.last_session_response)
        except IOError as ex:
            self.logger.error(f"Error dumping last session: {ex}")

        try:
            response = self.httpClient.get(
                constants.CREATE_SESSION % self.session_info.session_id,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": self.get_token_header(),
                    "x-xbl-contract-version": "107",
                },
                timeout=5,
            )
            self.storage_manager_.current_session_response(response.text)
        except (requests.RequestException, IOError) as ex:
            self.logger.error(f"Error dumping current session: {ex}")

    def add_sub_session(self, id: str) -> None:
        # Make sure we don't already have that ID
        if id in self.sub_session_managers:
            self.core_logger.error("Sub-session already exists with that ID")
            return

        try:
            sub_session_manager = SubSessionManager(
                id, self, self.storage_manager_.sub_session(id), self.notification_manager_, self.logger
            )
            sub_session_manager.init()
            sub_session_manager.friend_manager_.init(self.friend_sync_config)
            self.sub_session_managers[id] = sub_session_manager
        except (SessionCreationException, SessionUpdateException) as ex:
            self.core_logger.error("Failed to create sub-session", ex)
            return

        try:
            self.storage_manager_.sub_sessions(json.dumps(list(self.sub_session_managers.keys())))
        except (ValueError, IOError) as ex:
            self.core_logger.error("Failed to update sub-session list", ex)

    def remove_sub_session(self, id: str) -> None:
        if id not in self.sub_session_managers:
            self.core_logger.error("Sub-session does not exist with that ID")
            return

        self.sub_session_managers.pop(id).shutdown()

        try:
            self.storage_manager_.sub_session(id).cleanup()
        except IOError as ex:
            self.core_logger.error("Failed to delete sub-session cache file", ex)

        try:
            self.storage_manager_.sub_sessions(json.dumps(list(self.sub_session_managers.keys())))
        except (ValueError, IOError) as ex:
            self.core_logger.error("Failed to update sub-session list", ex)

        self.core_logger.info(f"Removed sub-session with ID {id}")

    def list_sessions(self) -> None:
        messages: list[str] = []
        self.core_logger.info("Loading status of sessions...")

        messages.append("Primary Session:")
        messages.append(f" - Gamertag: {self.get_gamertag()}")
        messages.append(
            f"   Following: {self.social_summary().target_following_count}/{constants.MAX_FRIENDS}"
        )

        if self.sub_session_managers:
            messages.append(f"Sub-sessions: ({len(self.sub_session_managers)})")
            for key, sub_session in self.sub_session_managers.items():
                messages.append(f" - ID: {key}")
                messages.append(f"   Gamertag: {sub_session.get_gamertag()}")
                messages.append(
                    f"   Following: {sub_session.social_summary().target_following_count}/"
                    f"{constants.MAX_FRIENDS}"
                )
        else:
            messages.append("No sub-sessions")

        for message in messages:
            self.core_logger.info(message)

    def restart_callback_(self, restart) -> None:
        self.restart_callback = restart

    def restart(self) -> None:
        if self.restart_callback is not None:
            self.restart_callback()
        else:
            self.logger.error("No restart callback set")
