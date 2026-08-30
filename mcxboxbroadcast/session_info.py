"""SessionInfo and ExpandedSessionInfo, ported from the Java classes."""

from __future__ import annotations

import random
import re
import uuid
from typing import Optional

from . import constants
from .config.core_config import SessionInfoConfig

COLOR_PATTERN = re.compile("\u00a7[\\w]")


def remove_color_codes(string: Optional[str]) -> str:
    if string is None:
        return ""
    return COLOR_PATTERN.sub("", string).strip()


class SessionInfo:
    def __init__(
        self,
        host_name: str = "",
        world_name: str = "",
        players: int = 0,
        max_players: int = 0,
        ip: str = "",
        port: int = 0,
    ) -> None:
        self._host_name = remove_color_codes(host_name)
        self._world_name = remove_color_codes(world_name)
        self._players = int(players)
        self._max_players = int(max_players)
        self._ip = ip
        self._port = int(port)

    @classmethod
    def from_config(cls, config: SessionInfoConfig) -> "SessionInfo":
        return cls(
            config.host_name,
            config.world_name,
            config.players,
            config.max_players,
            config.ip,
            config.port,
        )

    @property
    def host_name(self) -> str:
        return self._host_name

    @host_name.setter
    def host_name(self, value: str) -> None:
        self._host_name = remove_color_codes(value)

    @property
    def world_name(self) -> str:
        return self._world_name

    @world_name.setter
    def world_name(self, value: str) -> None:
        self._world_name = remove_color_codes(value)

    @property
    def version(self) -> str:
        return constants.MINECRAFT_VERSION

    @property
    def protocol(self) -> int:
        return constants.PROTOCOL_VERSION

    @property
    def players(self) -> int:
        # Allows the join button on 1.21.70 to show up
        if self._players <= 0:
            return 1
        return self._players

    @players.setter
    def players(self, value: int) -> None:
        self._players = int(value)

    @property
    def max_players(self) -> int:
        # Prevents the server from showing as full
        if self._max_players <= self.players:
            return self.players + 1
        return self._max_players

    @max_players.setter
    def max_players(self, value: int) -> None:
        self._max_players = int(value)

    @property
    def ip(self) -> str:
        return self._ip

    @ip.setter
    def ip(self, value: str) -> None:
        self._ip = value

    @property
    def port(self) -> int:
        return self._port

    @port.setter
    def port(self, value: int) -> None:
        self._port = int(value)

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
        super().__init__(
            session_info.host_name,
            session_info.world_name,
            session_info.players,
            session_info.max_players,
            session_info.ip,
            session_info.port,
        )
        self.connection_id = connection_id
        self.xuid = xuid
        self.rak_net_guid = ""
        self.session_id = str(uuid.uuid4())
        self.nether_net_id = abs(random.getrandbits(63))
        self.device_id = str(uuid.uuid4())
        self.pmsg_id: Optional[str] = None
        self.handle_id: Optional[str] = None

    def update_session_info(self, session_info: SessionInfo) -> None:
        self.host_name = session_info.host_name or "MCXboxBroadcast"
        self.world_name = session_info.world_name or self.host_name
        self.players = session_info.players
        self.max_players = session_info.max_players
        self.ip = session_info.ip
        self.port = session_info.port

    @property
    def host_name(self) -> str:
        return super().host_name if self._host_name else "MCXboxBroadcast"

    @host_name.setter
    def host_name(self, value: str) -> None:
        self._host_name = remove_color_codes(value)

    @property
    def world_name(self) -> str:
        return super().world_name if self._world_name else self.host_name
