"""Session info models, mirroring the Java ``SessionInfo`` / ``ExpandedSessionInfo``."""

from __future__ import annotations

import random
import re
import uuid
from typing import Optional

from mcxboxbroadcast.config.core_config import SessionInfoConfig
from mcxboxbroadcast.constants import BEDROCK_PROTOCOL_VERSION, BEDROCK_VERSION

_COLOR_PATTERN = re.compile(r"\u00A7[\w]")


def _remove_color_codes(string: Optional[str]) -> str:
    if string is None:
        return ""
    return _COLOR_PATTERN.sub("", string).strip()


class SessionInfo:
    def __init__(
        self,
        host_name: str = "",
        world_name: str = "",
        players: int = 0,
        max_players: int = 20,
        ip: str = "",
        port: int = 19132,
    ) -> None:
        self._host_name = host_name
        self._world_name = world_name
        self._players = players
        self._max_players = max_players
        self._ip = ip
        self._port = port

    @classmethod
    def from_config(cls, config: SessionInfoConfig) -> "SessionInfo":
        return cls(
            host_name=config.host_name,
            world_name=config.world_name,
            players=config.players,
            max_players=config.max_players,
            ip=config.ip,
            port=config.port,
        )

    # -- getters / setters ------------------------------------------------
    def get_host_name(self) -> str:
        return self._host_name

    def set_host_name(self, host_name: str) -> None:
        self._host_name = _remove_color_codes(host_name)

    def get_world_name(self) -> str:
        return self._world_name

    def set_world_name(self, world_name: str) -> None:
        self._world_name = _remove_color_codes(world_name)

    def get_version(self) -> str:
        return BEDROCK_VERSION

    def get_protocol(self) -> int:
        return BEDROCK_PROTOCOL_VERSION

    def get_players(self) -> int:
        # Allows the join button on 1.21.70 to show up
        if self._players <= 0:
            return 1
        return self._players

    def set_players(self, players: int) -> None:
        self._players = players

    def get_max_players(self) -> int:
        # Prevents the server from showing as full
        if self._max_players <= self.get_players():
            return self.get_players() + 1
        return self._max_players

    def set_max_players(self, max_players: int) -> None:
        self._max_players = max_players

    def get_ip(self) -> str:
        return self._ip

    def set_ip(self, ip: str) -> None:
        self._ip = ip

    def get_port(self) -> int:
        return self._port

    def set_port(self, port: int) -> None:
        self._port = port

    def copy(self) -> "SessionInfo":
        return SessionInfo(
            self._host_name,
            self._world_name,
            self._players,
            self._max_players,
            self._ip,
            self._port,
        )


class ExpandedSessionInfo(SessionInfo):
    def __init__(self, connection_id: str, xuid: str, session_info: SessionInfo) -> None:
        super().__init__()
        self._connection_id = connection_id
        self._xuid = xuid
        self._rak_net_guid = ""

        self._session_id = str(uuid.uuid4())
        self._nether_net_id = abs(random.getrandbits(63))
        self._device_id = str(uuid.uuid4())
        self._handle_id: Optional[str] = None
        self._pmsg_id: Optional[str] = None

        self.set_host_name(session_info.get_host_name() if session_info.get_host_name() else "MCXboxBroadcast")
        self.set_world_name(session_info.get_world_name() if session_info.get_world_name() else self.get_host_name())
        self.set_players(session_info.get_players())
        self.set_max_players(session_info.get_max_players())
        self.set_ip(session_info.get_ip())
        self.set_port(session_info.get_port())

    def update_session_info(self, session_info: SessionInfo) -> None:
        self.set_host_name(session_info.get_host_name() if session_info.get_host_name() else "MCXboxBroadcast")
        self.set_world_name(session_info.get_world_name() if session_info.get_world_name() else self.get_host_name())
        self.set_players(session_info.get_players())
        self.set_max_players(session_info.get_max_players())
        self.set_ip(session_info.get_ip())
        self.set_port(session_info.get_port())

    # -- expanded getters / setters --------------------------------------
    def get_connection_id(self) -> str:
        return self._connection_id

    def set_connection_id(self, connection_id: str) -> None:
        self._connection_id = connection_id

    def get_xuid(self) -> str:
        return self._xuid

    def set_xuid(self, xuid: str) -> None:
        self._xuid = xuid

    def get_rak_net_guid(self) -> str:
        return self._rak_net_guid

    def set_rak_net_guid(self, rak_net_guid: str) -> None:
        self._rak_net_guid = rak_net_guid

    def get_session_id(self) -> str:
        return self._session_id

    def set_session_id(self, session_id: str) -> None:
        self._session_id = session_id

    def get_nether_net_id(self) -> int:
        return self._nether_net_id

    def get_device_id(self) -> str:
        return self._device_id

    def get_handle_id(self) -> Optional[str]:
        return self._handle_id

    def set_handle_id(self, handle_id: str) -> None:
        self._handle_id = handle_id

    def get_pmsg_id(self) -> Optional[str]:
        return self._pmsg_id

    def set_pmsg_id(self, pmsg_id: str) -> None:
        self._pmsg_id = pmsg_id
