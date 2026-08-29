"""Primary session manager, mirroring the Java ``SessionManager``.

Manages the main Xbox LIVE session, sub-sessions, nonces for joining and the
scheduled background tasks (presence heartbeat, friend sync, etc).
"""

from __future__ import annotations

import json
import os
import threading
from typing import Callable, Optional

from mcxboxbroadcast.config.core_config import FriendSyncConfig
from mcxboxbroadcast.constants import (
    CREATE_SESSION,
    MAX_FRIENDS,
    gson_dumps,
    gson_loads,
)
from mcxboxbroadcast.exceptions import (
    SessionCreationException,
    SessionUpdateException,
)
from mcxboxbroadcast.logger import Logger
from mcxboxbroadcast.models.session import CreateSessionRequest, CreateSessionResponse
from mcxboxbroadcast.notifications import NotificationManager
from mcxboxbroadcast.scheduled_executor import ScheduledExecutor
from mcxboxbroadcast.session_info import ExpandedSessionInfo, SessionInfo
from mcxboxbroadcast.session_manager_core import SessionManagerCore
from mcxboxbroadcast.storage import StorageManager


class SessionManager(SessionManagerCore):
    def __init__(
        self,
        storage_manager: StorageManager,
        notification_manager: NotificationManager,
        logger: Logger,
    ) -> None:
        super().__init__(storage_manager, notification_manager, logger.prefixed("Primary Session"))

        self._scheduled_executor = ScheduledExecutor("MCXboxBroadcast", 5)
        self._sub_session_managers: dict = {}
        self._friend_sync_config: Optional[FriendSyncConfig] = None
        self._restart_callback: Optional[Callable[[], None]] = None
        self._nonces: dict = {}
        self._visibility: str = "friends"

    # ------------------------------------------------------------------
    # Core overrides
    # ------------------------------------------------------------------
    def scheduled_thread(self) -> ScheduledExecutor:
        return self._scheduled_executor

    def get_session_id(self) -> str:
        return self._session_info.get_session_id()

    def session_info(self) -> ExpandedSessionInfo:
        return self._session_info

    def friend_sync_config(self) -> Optional[FriendSyncConfig]:
        return self._friend_sync_config

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------
    def init(
        self,
        session_info: SessionInfo,
        friend_sync_config: FriendSyncConfig,
        visibility: str = "friends",
    ) -> bool:
        """Initialize the session manager with the given session information."""
        # Set the internal session information based on the session info
        self._session_info = ExpandedSessionInfo("", "", session_info)
        self._visibility = visibility if visibility in ("friends", "public") else "friends"

        super().init()

        # If we failed to initialize, don't continue with the rest of the setup
        if not self._initialized:
            return self._initialized

        # Set up the auto friend sync
        self._friend_sync_config = friend_sync_config
        self.friend_manager().init(friend_sync_config)

        # Load sub-sessions from cache
        sub_sessions: list = []
        try:
            sub_sessions_json = self.storage_manager().sub_sessions()
            if sub_sessions_json:
                sub_sessions = json.loads(sub_sessions_json)
        except Exception:
            pass

        # Create the sub-sessions in a new thread so we don't block the main thread
        final_sub_sessions = list(sub_sessions)

        def _create_sub_sessions() -> None:
            from mcxboxbroadcast.sub_session_manager import SubSessionManager

            for sub_session in final_sub_sessions:
                try:
                    sub_session_manager = SubSessionManager(
                        sub_session,
                        self,
                        self.storage_manager().sub_session(sub_session),
                        self.notification_manager(),
                        self.logger(),
                    )
                    sub_session_manager.init()
                    sub_session_manager.friend_manager().init(friend_sync_config)
                    self._sub_session_managers[sub_session] = sub_session_manager
                except (SessionCreationException, SessionUpdateException) as e:
                    self.logger().error(f"Failed to create sub-session {sub_session}: {e}")
                    # TODO Retry creation after 30s or so

        threading.Thread(target=_create_sub_sessions, name="Sub-Session Init", daemon=True).start()

        return self._initialized

    def handle_friendship(self) -> bool:
        # Don't do anything as we are the main session
        return False

    # ------------------------------------------------------------------
    # Session updates
    # ------------------------------------------------------------------
    def update_session(self, session_info: SessionInfo) -> None:
        """Update the current session with new information."""
        self._session_info.update_session_info(session_info)
        self._update_session()

    def _update_session(self) -> None:
        # Make sure the websocket connection is still active
        self.check_connection()

        response_body = self.update_session_internal(
            CREATE_SESSION % self.get_session_id(),
            CreateSessionRequest(self._session_info, self._nonces, self._visibility),
        )

        try:
            session_response = gson_loads(response_body)
            members = (session_response or {}).get("members", {}) or {}

            # Restart if we have 28/30 session members
            players = len(members)
            if players >= 28:
                self.logger().info(f"Restarting session due to {players}/30 players")
                self.restart()
        except (ValueError, TypeError) as e:
            raise SessionUpdateException(f"Failed to parse session response: {e}") from e

    # ------------------------------------------------------------------
    # Nonces
    # ------------------------------------------------------------------
    def update_nonces(self) -> None:
        """Update the nonces in the session based on the current players."""
        try:
            response = self._http_client.get(
                CREATE_SESSION % self.get_session_id(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": self.get_token_header(),
                    "x-xbl-contract-version": "107",
                },
                timeout=30,
            )
        except Exception as e:
            raise SessionUpdateException(
                f"Failed to get session for nonces, joining will not work: {e}"
            ) from e

        try:
            session_response = gson_loads(response.text)
        except ValueError as e:
            raise SessionUpdateException(
                f"Failed to get session for nonces, joining will not work: {e}"
            ) from e

        if not session_response:
            raise SessionUpdateException(
                "Failed to get session for nonces, joining will not work: sessionResponse is null"
            )

        has_changes = False

        # Collect active XUIDs from the session
        active_xuids = set()
        members = session_response.get("members", {}) or {}
        for member in members.values():
            system = (member.get("constants", {}) or {}).get("system", {})
            xuid = system.get("xuid")
            if xuid:
                active_xuids.add(xuid)

        # Remove our own xuid
        active_xuids.discard(self._session_info.get_xuid())

        # Remove stale nonces
        stale = set(self._nonces.keys()) - active_xuids
        if stale:
            for key in stale:
                del self._nonces[key]
            has_changes = True

        for xuid in active_xuids:
            if xuid not in self._nonces:
                # Generate a nonce (16 hex chars from 8 random bytes)
                nonce = "".join(f"{b:02x}" for b in os.urandom(8))
                self._nonces[xuid] = nonce
                self.logger().debug(f"Generated nonce for XUID {xuid}: {nonce}")
                has_changes = True

        # Only update the session properties if something changed
        if has_changes:
            self._update_session()

    # ------------------------------------------------------------------
    # Sub-sessions
    # ------------------------------------------------------------------
    def add_sub_session(self, session_id: str) -> None:
        from mcxboxbroadcast.sub_session_manager import SubSessionManager

        # Make sure we don't already have that ID
        if session_id in self._sub_session_managers:
            self._logger.error("Sub-session already exists with that ID")
            return

        # Create the sub-session manager
        try:
            sub_session_manager = SubSessionManager(
                session_id,
                self,
                self.storage_manager().sub_session(session_id),
                self.notification_manager(),
                self.logger(),
            )
            sub_session_manager.init()
            sub_session_manager.friend_manager().init(self._friend_sync_config)
            self._sub_session_managers[session_id] = sub_session_manager
        except (SessionCreationException, SessionUpdateException) as e:
            self._logger.error(f"Failed to create sub-session: {e}")
            return

        # Update the list of sub-sessions
        try:
            self.storage_manager().sub_sessions(gson_dumps(list(self._sub_session_managers.keys())))
        except Exception as e:
            self._logger.error(f"Failed to update sub-session list: {e}")

    def remove_sub_session(self, session_id: str) -> None:
        # Make sure we have that ID
        if session_id not in self._sub_session_managers:
            self._logger.error("Sub-session does not exist with that ID")
            return

        # Remove the sub-session manager
        self._sub_session_managers[session_id].shutdown()
        del self._sub_session_managers[session_id]

        # Delete the sub-session cache file
        try:
            self.storage_manager().sub_session(session_id).cleanup()
        except Exception as e:
            self._logger.error(f"Failed to delete sub-session cache file: {e}")

        # Update the list of sub-sessions
        try:
            self.storage_manager().sub_sessions(gson_dumps(list(self._sub_session_managers.keys())))
        except Exception as e:
            self._logger.error(f"Failed to update sub-session list: {e}")

        self._logger.info(f"Removed sub-session with ID {session_id}")

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        # Shutdown all sub-sessions
        for sub_session_manager in self._sub_session_managers.values():
            sub_session_manager.shutdown()

        # Shutdown self
        super().shutdown()
        self._scheduled_executor.shutdown()

    def dump_session(self) -> None:
        """Dump the current and last session responses to json files."""
        try:
            self.storage_manager().last_session_response(self.last_session_response())
        except Exception as e:
            self.logger().error(f"Error dumping last session: {e}")

        try:
            response = self._http_client.get(
                CREATE_SESSION % self.get_session_id(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": self.get_token_header(),
                    "x-xbl-contract-version": "107",
                },
                timeout=30,
            )
            self.storage_manager().current_session_response(response.text)
        except Exception as e:
            self.logger().error(f"Error dumping current session: {e}")

    def list_sessions(self) -> None:
        """List all sessions and their information."""
        core_logger = self._logger.prefixed("")
        messages = []
        core_logger.info("Loading status of sessions...")

        messages.append("Primary Session:")
        messages.append(f" - Gamertag: {self.get_gamertag()}")
        messages.append(f"   Following: {self.social_summary().targetFollowingCount}/{MAX_FRIENDS}")

        if self._sub_session_managers:
            messages.append(f"Sub-sessions: ({len(self._sub_session_managers)})")
            for sub_session_id, sub_session in self._sub_session_managers.items():
                messages.append(f" - ID: {sub_session_id}")
                messages.append(f"   Gamertag: {sub_session.get_gamertag()}")
                messages.append(
                    f"   Following: {sub_session.social_summary().targetFollowingCount}/{MAX_FRIENDS}"
                )
        else:
            messages.append("No sub-sessions")

        for message in messages:
            core_logger.info(message)

    def restart_callback(self, callback: Callable[[], None]) -> None:
        """Set the callback to run when the session manager needs to be restarted."""
        self._restart_callback = callback

    def restart(self) -> None:
        """Restart the session manager."""
        if self._restart_callback is not None:
            self._restart_callback()
        else:
            self.logger().error("No restart callback set")
