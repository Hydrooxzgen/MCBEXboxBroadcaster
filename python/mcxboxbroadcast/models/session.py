"""Session directory models, mirroring ``core/models/session``.

Field names are kept exactly as the Java records declare them (camelCase) so
that :func:`mcxboxbroadcast.constants.gson_dumps` produces the payloads the
Xbox Live session directory expects.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SessionRef:
    scid: str
    templateName: str
    name: str


@dataclass
class SessionSystemProperties:
    joinRestriction: str = "followed"
    readRestriction: str = "followed"
    closed: bool = False


@dataclass
class Connection:
    ConnectionType: int
    HostIpAddress: str
    HostPort: int
    NetherNetId: int
    PmsgId: Optional[str] = None

    def __init__(self, netherNetId: int, pmsgId: Optional[str] = None) -> None:
        from mcxboxbroadcast.constants import CONNECTION_TYPE_JSON_RPC

        self.ConnectionType = CONNECTION_TYPE_JSON_RPC
        self.HostIpAddress = ""
        self.HostPort = 0
        self.NetherNetId = netherNetId
        self.PmsgId = pmsgId


@dataclass
class SessionCustomProperties:
    BroadcastSetting: int
    CrossPlayDisabled: bool
    Joinability: str
    LanGame: bool
    MaxMemberCount: int
    MemberCount: int
    OnlineCrossPlatformGame: bool
    SupportedConnections: list
    TitleId: int
    TransportLayer: int
    levelId: str
    hostName: str
    ownerId: str
    rakNetGUID: str
    worldName: str
    worldType: str  # Survival, Creative, Adventure
    protocol: int
    version: str
    isEditorWorld: bool
    isHardcore: bool  # If true then shows as hardcore
    nonces: dict = field(default_factory=dict)


@dataclass
class SessionProperties:
    system: SessionSystemProperties
    custom: SessionCustomProperties


@dataclass
class MemberSubscription:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    changeTypes: list = field(default_factory=lambda: ["everything"])


@dataclass
class MemberConstantsSystem:
    xuid: str
    initialize: bool = True


@dataclass
class MemberPropertiesSystem:
    active: bool
    connection: str
    subscription: MemberSubscription


@dataclass
class SessionMember:
    joinTime: Any  # Instant / None
    constants: dict  # { "system": MemberConstantsSystem }
    gamertag: Optional[str]
    properties: dict  # { "system": MemberPropertiesSystem }


@dataclass
class JoinSessionRequest:
    members: dict = field(default_factory=dict)

    def __init__(self, sessionInfo: "object") -> None:
        from mcxboxbroadcast.models.session import MemberConstantsSystem, MemberPropertiesSystem, MemberSubscription, SessionMember

        constants = {
            "system": MemberConstantsSystem(sessionInfo.get_xuid(), True),
        }
        properties = {
            "system": MemberPropertiesSystem(True, sessionInfo.get_connection_id(), MemberSubscription()),
        }
        self.members = {
            "me": SessionMember(None, constants, None, properties),
        }


@dataclass
class CreateSessionRequest(JoinSessionRequest):
    properties: SessionProperties = field(default_factory=SessionProperties)

    def __init__(self, sessionInfo: "object", nonces: dict, visibility: str = "friends") -> None:
        super().__init__(sessionInfo)
        from mcxboxbroadcast.models.session import (
            Connection,
            SessionCustomProperties,
            SessionProperties,
            SessionSystemProperties,
        )

        # Only friends can see/join by default; "public" opens the session up
        # to everyone that can find it.
        if visibility == "public":
            join_restriction = "everyone"
            read_restriction = "everyone"
            joinability = "joinable_by_everyone"
        else:
            join_restriction = "followed"
            read_restriction = "followed"
            joinability = "joinable_by_friends"

        self.properties = SessionProperties(
            SessionSystemProperties(join_restriction, read_restriction),
            SessionCustomProperties(
                BroadcastSetting=3,
                CrossPlayDisabled=False,
                Joinability=joinability,
                LanGame=False,
                MaxMemberCount=sessionInfo.get_max_players(),
                MemberCount=sessionInfo.get_players(),
                OnlineCrossPlatformGame=True,
                SupportedConnections=[
                    Connection(sessionInfo.get_nether_net_id(), sessionInfo.get_pmsg_id())
                ],
                TitleId=0,
                TransportLayer=2,
                levelId="level",
                hostName=sessionInfo.get_host_name(),
                ownerId=sessionInfo.get_xuid(),
                rakNetGUID="",
                worldName=sessionInfo.get_world_name(),
                worldType="Survival",
                protocol=sessionInfo.get_protocol(),
                version=sessionInfo.get_version(),
                isEditorWorld=False,
                isHardcore=False,
                nonces=nonces,
            ),
        )


@dataclass
class CreateHandleRequest:
    version: int
    type: str
    sessionRef: SessionRef
    invitedXuid: Optional[str] = None
    inviteAttributes: Optional[dict] = None


@dataclass
class CreateHandleResponse:
    createTime: Optional[str] = None
    gameTypes: Any = None
    id: Optional[str] = None
    inviteProtocol: Optional[str] = None
    ownerXuid: Optional[str] = None
    sessionRef: Optional[SessionRef] = None
    titleId: Optional[str] = None
    type: Optional[str] = None
    version: int = 0


@dataclass
class CreateSessionResponse:
    branch: Optional[str] = None
    changeNumber: int = 0
    constants: Any = None
    contractVersion: int = 0
    correlationId: Optional[str] = None
    members: dict = field(default_factory=dict)
    membersInfo: Any = None
    properties: Any = None
    servers: Any = None
    startTime: Optional[str] = None


@dataclass
class SocialSummaryResponse:
    targetFollowingCount: int = -1
    targetFollowerCount: int = -1
    isCallerFollowingTarget: bool = False
    isTargetFollowingCaller: bool = False
    hasCallerMarkedTargetAsFavorite: bool = False
    hasCallerMarkedTargetAsKnown: bool = False
    legacyFriendStatus: str = ""
    availablePeopleSlots: int = -1
    recentChangeCount: int = -1
    watermark: str = ""


@dataclass
class FollowerResponse:
    people: Optional[list] = None  # list[Person]
    recommendationSummary: Any = None
    friendFinderState: Any = None
    accountLinkDetails: Any = None

    @dataclass
    class LinkedAccount:
        networkName: Optional[str] = None
        displayName: Optional[str] = None
        showOnProfile: bool = False
        isFamilyFriendly: bool = False
        deeplink: Any = None

    @dataclass
    class Person:
        xuid: Optional[str] = None
        isFavorite: bool = False
        isFollowingCaller: bool = False
        isFollowedByCaller: bool = False
        isIdentityShared: bool = False
        addedDateTimeUtc: Any = None
        displayName: Optional[str] = None
        realName: Optional[str] = None
        displayPicRaw: Optional[str] = None
        showUserAsAvatar: Optional[str] = None
        gamertag: Optional[str] = None
        gamerScore: Optional[str] = None
        modernGamertag: Optional[str] = None
        modernGamertagSuffix: Optional[str] = None
        uniqueModernGamertag: Optional[str] = None
        xboxOneRep: Optional[str] = None
        presenceState: Optional[str] = None
        presenceText: Optional[str] = None
        presenceDevices: Any = None
        isBroadcasting: bool = False
        isCloaked: Any = None
        isQuarantined: bool = False
        isXbox360Gamerpic: bool = False
        lastSeenDateTimeUtc: Any = None
        suggestion: Any = None
        recommendation: Any = None
        search: Any = None
        titleHistory: Any = None
        multiplayerSummary: Any = None
        recentPlayer: Any = None
        follower: Any = None
        preferredColor: Any = None
        presenceDetails: Any = None
        titlePresence: Any = None
        titleSummaries: Any = None
        presenceTitleIds: Any = None
        detail: Any = None
        communityManagerTitles: Any = None
        socialManager: Any = None
        broadcast: Any = None
        avatar: Any = None
        linkedAccounts: Optional[list] = None
        colorTheme: Optional[str] = None
        preferredFlag: Optional[str] = None
        preferredPlatforms: Optional[list] = None

        def merge(self, person: "FollowerResponse.Person") -> "FollowerResponse.Person":
            """Merge some fields from another Person into this one.

            Mirrors the Java ``merge``: addedDateTimeUtc, follower,
            isFollowedByCaller, isFollowingCaller.
            """
            if person.addedDateTimeUtc is not None:
                self.addedDateTimeUtc = person.addedDateTimeUtc
            if person.follower is not None:
                self.follower = person.follower
            if person.isFollowedByCaller:
                self.isFollowedByCaller = True
            if person.isFollowingCaller:
                self.isFollowingCaller = True
            return self


__all__ = [
    "SessionRef",
    "SessionSystemProperties",
    "Connection",
    "SessionCustomProperties",
    "SessionProperties",
    "MemberSubscription",
    "MemberConstantsSystem",
    "MemberPropertiesSystem",
    "SessionMember",
    "JoinSessionRequest",
    "CreateSessionRequest",
    "CreateHandleRequest",
    "CreateHandleResponse",
    "CreateSessionResponse",
    "SocialSummaryResponse",
    "FollowerResponse",
]
