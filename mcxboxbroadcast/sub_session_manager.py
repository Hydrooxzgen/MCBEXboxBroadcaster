"""Sub-session manager, ported from Java SubSessionManager.java."""

from __future__ import annotations

from . import constants
from .logger import Logger
from .models.session import JoinSessionRequest
from .notifications.notification_manager import NotificationManager
from .session_manager_core import SessionManagerCore
from .storage.storage_manager import StorageManager


class SubSessionManager(SessionManagerCore):
    def __init__(
        self,
        id: str,
        parent,  # SessionManager
        storage_manager: StorageManager,
        notification_manager: NotificationManager,
        logger: Logger,
    ) -> None:
        super().__init__(storage_manager, notification_manager, logger.prefixed(f"Sub-Session {id}"))
        self.parent = parent

    def scheduled_thread(self):
        return self.parent.scheduled_thread()

    def nether_net_port_range(self):
        return self.parent.nether_net_port_range()

    def get_session_id(self) -> str:
        return self.parent.session_info.session_id

    def handle_friendship(self) -> bool:
        # Add the main account
        sub_add = self.friend_manager_.add_if_required(
            self.parent.get_xuid(), self.parent.get_gamertag()
        )

        # Get the main account to add us
        main_add = self.parent.friend_manager_.add_if_required(self.get_xuid(), self.get_gamertag())

        return sub_add or main_add

    def update_session(self) -> None:
        self.update_session_internal(
            constants.JOIN_SESSION % self.parent.session_info.handle_id,
            JoinSessionRequest(self.parent.session_info.xuid, self.parent.session_info.connection_id),
        )
