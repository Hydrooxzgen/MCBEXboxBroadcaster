"""Sub-session manager, mirroring the Java ``SubSessionManager``.

Joins an additional account into the parent's session so friends of that
account can also see and join the server.
"""

from __future__ import annotations

from mcxboxbroadcast.constants import JOIN_SESSION
from mcxboxbroadcast.exceptions import SessionUpdateException
from mcxboxbroadcast.logger import Logger
from mcxboxbroadcast.models.session import JoinSessionRequest
from mcxboxbroadcast.notifications import NotificationManager
from mcxboxbroadcast.scheduled_executor import ScheduledExecutor
from mcxboxbroadcast.session_manager import SessionManager
from mcxboxbroadcast.session_manager_core import SessionManagerCore
from mcxboxbroadcast.storage import StorageManager


class SubSessionManager(SessionManagerCore):
    def __init__(
        self,
        session_id: str,
        parent: SessionManager,
        storage_manager: StorageManager,
        notification_manager: NotificationManager,
        logger: Logger,
    ) -> None:
        super().__init__(
            storage_manager,
            notification_manager,
            logger.prefixed(f"Sub-Session {session_id}"),
        )
        self._parent = parent

    # ------------------------------------------------------------------
    # Core overrides
    # ------------------------------------------------------------------
    def scheduled_thread(self) -> ScheduledExecutor:
        return self._parent.scheduled_thread()

    def get_session_id(self) -> str:
        return self._parent.session_info().get_session_id()

    def handle_friendship(self) -> bool:
        # TODO Some form of force flag just in case the master friends list is full

        # Add the main account
        sub_add = self.friend_manager().add_if_required(
            self._parent.get_xuid(), self._parent.get_gamertag()
        )

        # Get the main account to add us
        main_add = self._parent.friend_manager().add_if_required(
            self.get_xuid(), self.get_gamertag()
        )

        return sub_add or main_add

    def update_session(self) -> None:
        self.update_session_internal(
            JOIN_SESSION % self._parent.session_info().get_handle_id(),
            JoinSessionRequest(self._parent.session_info()),
        )
