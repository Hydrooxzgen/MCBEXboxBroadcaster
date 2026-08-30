"""Session models, ported from the Java core.models.session package."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .. import constants


@dataclass
class SessionRef:
    scid: str
    template_name: str
    name: str

    def to_json(self) -> dict:
        return {
            "scid": self.scid,
            "templateName": self.template_name,
            "name": self.name,
        }


@dataclass
class Connection:
    connection_type: int
    host_ip_address: str
    host_port: int
    nether_net_id: int
    pmsg_id: Optional[str] = None

    @classmethod
    def json_rpc(cls, nether_net_id: int, pmsg_id: Optional[str]) -> "Connection":
        return cls(
            constants.CONNECTION_TYPE_JSON_RPC,
            "",
            0,
            nether_net_id,
            pmsg_id,
        )

    def to_json(self) -> dict:
        return {
            "ConnectionType": self.connection_type,
            "HostIpAddress": self.host_ip_address,
            "HostPort": self.host_port,
            "NetherNetId": self.nether_net_id,
            "PmsgId": self.pmsg_id,
        }


@dataclass
class MemberSubscription:
    id: str
    change_types: list[str]

    def __init__(self) -> None:
        import uuid

        self.id = str(uuid.uuid4())
        self.change_types = ["everything"]

    def to_json(self) -> dict:
        return {"id": self.id, "changeTypes": self.change_types}


@dataclass
class MemberConstantsSystem:
    xuid: str
    initialize: bool

    def to_json(self) -> dict:
        return {"xuid": self.xuid, "initialize": self.initialize}


@dataclass
class MemberPropertiesSystem:
    active: bool
    connection: str
    subscription: MemberSubscription = field(default_factory=MemberSubscription)

    def to_json(self) -> dict:
        return {
            "active": self.active,
            "connection": self.connection,
            "subscription": self.subscription.to_json(),
        }


@dataclass
class SessionMember:
    constants: dict[str, MemberConstantsSystem]
    properties: dict[str, MemberPropertiesSystem]
    join_time: Optional[datetime] = None
    gamertag: Optional[str] = None

    @classmethod
    def from_json(cls, json_data: dict) -> "SessionMember":
        join_time = None
        if json_data.get("joinTime"):
            join_time = parse_xbox_datetime(json_data["joinTime"])
        return cls(
            join_time=join_time,
            gamertag=json_data.get("gamertag"),
            constants=json_data.get("constants", {}),
            properties=json_data.get("properties", {}),
        )


@dataclass
class SessionSystemProperties:
    join_restriction: str = "followed"
    read_restriction: str = "followed"
    closed: bool = False


@dataclass
class SessionCustomProperties:
    broadcast_setting: int
    cross_play_disabled: bool
    joinability: str
    lan_game: bool
    max_member_count: int
    member_count: int
    online_cross_platform_game: bool
    supported_connections: list[Connection]
    title_id: int
    transport_layer: int
    level_id: str
    host_name: str
    owner_id: str
    rak_net_guid: str
    world_name: str
    world_type: str  # Survival, Creative, Adventure
    protocol: int
    version: str
    is_editor_world: bool
    is_hardcore: bool
    nonces: dict[str, str]

    def to_json(self) -> dict:
        return {
            "BroadcastSetting": self.broadcast_setting,
            "CrossPlayDisabled": self.cross_play_disabled,
            "Joinability": self.joinability,
            "LanGame": self.lan_game,
            "MaxMemberCount": self.max_member_count,
            "MemberCount": self.member_count,
            "OnlineCrossPlatformGame": self.online_cross_platform_game,
            "SupportedConnections": [c.to_json() for c in self.supported_connections],
            "TitleId": self.title_id,
            "TransportLayer": self.transport_layer,
            "levelId": self.level_id,
            "hostName": self.host_name,
            "ownerId": self.owner_id,
            "rakNetGUID": self.rak_net_guid,
            "worldName": self.world_name,
            "worldType": self.world_type,
            "protocol": self.protocol,
            "version": self.version,
            "isEditorWorld": self.is_editor_world,
            "isHardcore": self.is_hardcore,
            "nonces": self.nonces,
        }


@dataclass
class SessionProperties:
    system: SessionSystemProperties
    custom: SessionCustomProperties

    def to_json(self) -> dict:
        out = {"custom": self.custom.to_json()}
        if self.system is not None:
            out["system"] = {
                "joinRestriction": self.system.join_restriction,
                "readRestriction": self.system.read_restriction,
                "closed": self.system.closed,
            }
        return out


@dataclass
class JoinSessionRequest:
    members: dict[str, SessionMember]

    def __init__(self, xuid: str, connection_id: str) -> None:
        self.members = {
            "me": SessionMember(
                constants={"system": MemberConstantsSystem(xuid, True)},
                properties={
                    "system": MemberPropertiesSystem(True, connection_id)
                },
            )
        }

    def to_json(self) -> dict:
        return {
            "members": {
                key: {
                    "constants": {k: v.to_json() for k, v in member.constants.items()},
                    "properties": {
                        k: v.to_json() for k, v in member.properties.items()
                    },
                }
                for key, member in self.members.items()
            }
        }


@dataclass
class CreateSessionRequest(JoinSessionRequest):
    properties: SessionProperties

    # NOTE: "friends of friends" joinability is NOT supported by the Xbox
    # MinecraftLobby template - the session directory only exposes the session
    # to the owner's direct friends (followed), and the template's
    # 'userAuthorizationStyle' capability forbids loosening the restrictions.
    # Verified experimentally: FOF players cannot see or join the session.
    def __init__(self, session_info, nonces: dict[str, str]) -> None:
        # session_info is an ExpandedSessionInfo
        super().__init__(session_info.xuid, session_info.connection_id)
        self.properties = SessionProperties(
            SessionSystemProperties(),
            SessionCustomProperties(
                broadcast_setting=3,
                cross_play_disabled=False,
                joinability="joinable_by_friends",
                lan_game=False,
                max_member_count=session_info.max_players,
                member_count=session_info.players,
                online_cross_platform_game=True,
                supported_connections=[
                    Connection.json_rpc(session_info.nether_net_id, session_info.pmsg_id)
                ],
                title_id=0,
                transport_layer=2,
                level_id="level",
                host_name=session_info.host_name,
                owner_id=session_info.xuid,
                rak_net_guid="",
                world_name=session_info.world_name,
                world_type="Survival",
                protocol=session_info.protocol,
                version=session_info.version,
                is_editor_world=False,
                is_hardcore=False,
                nonces=nonces,
            ),
        )

    def to_json(self) -> dict:
        base = super().to_json()
        base["properties"] = self.properties.to_json()
        return base


@dataclass
class CreateHandleRequest:
    version: int
    type: str
    session_ref: SessionRef
    invited_xuid: Optional[str] = None
    invite_attributes: Optional[dict[str, str]] = None

    def to_json(self) -> dict:
        out: dict[str, Any] = {
            "version": self.version,
            "type": self.type,
            "sessionRef": self.session_ref.to_json(),
        }
        if self.invited_xuid is not None:
            out["invitedXuid"] = self.invited_xuid
        if self.invite_attributes is not None:
            out["inviteAttributes"] = self.invite_attributes
        return out


@dataclass
class CreateHandleResponse:
    id: Optional[str]
    session_ref: Optional[SessionRef]
    owner_xuid: Optional[str]
    type: Optional[str]
    version: Optional[int]

    @classmethod
    def from_json(cls, json_data: dict) -> "CreateHandleResponse":
        ref = json_data.get("sessionRef")
        return cls(
            id=json_data.get("id"),
            session_ref=SessionRef(
                ref.get("scid", ""), ref.get("templateName", ""), ref.get("name", "")
            )
            if ref
            else None,
            owner_xuid=json_data.get("ownerXuid"),
            type=json_data.get("type"),
            version=json_data.get("version"),
        )


def _empty_members() -> dict[str, SessionMember]:
    return {}


@dataclass
class CreateSessionResponse:
    members: dict[str, SessionMember] = field(default_factory=_empty_members)
    branch: Optional[str] = None
    change_number: Optional[int] = None
    contract_version: Optional[int] = None
    correlation_id: Optional[str] = None
    properties: Optional[dict] = None
    start_time: Optional[str] = None

    @classmethod
    def from_json(cls, json_data: dict) -> "CreateSessionResponse":
        members = {}
        for key, member in (json_data.get("members") or {}).items():
            members[key] = SessionMember.from_json(member)
        return cls(
            members=members,
            branch=json_data.get("branch"),
            change_number=json_data.get("changeNumber"),
            contract_version=json_data.get("contractVersion"),
            correlation_id=json_data.get("correlationId"),
            properties=json_data.get("properties"),
            start_time=json_data.get("startTime"),
        )


@dataclass
class SocialSummaryResponse:
    target_following_count: int
    target_follower_count: int
    is_caller_following_target: bool
    is_target_following_caller: bool
    has_caller_marked_target_as_favorite: bool
    has_caller_marked_target_as_known: bool
    legacy_friend_status: str
    available_people_slots: int
    recent_change_count: int
    watermark: str

    @classmethod
    def from_json(cls, json_data: dict) -> "SocialSummaryResponse":
        return cls(
            target_following_count=json_data.get("targetFollowingCount", -1),
            target_follower_count=json_data.get("targetFollowerCount", -1),
            is_caller_following_target=json_data.get("isCallerFollowingTarget", False),
            is_target_following_caller=json_data.get("isTargetFollowingCaller", False),
            has_caller_marked_target_as_favorite=json_data.get(
                "hasCallerMarkedTargetAsFavorite", False
            ),
            has_caller_marked_target_as_known=json_data.get(
                "hasCallerMarkedTargetAsKnown", False
            ),
            legacy_friend_status=json_data.get("legacyFriendStatus", ""),
            available_people_slots=json_data.get("availablePeopleSlots", -1),
            recent_change_count=json_data.get("recentChangeCount", -1),
            watermark=json_data.get("watermark", ""),
        )

    @classmethod
    def empty(cls) -> "SocialSummaryResponse":
        return cls(-1, -1, False, False, False, False, "", -1, -1, "")


# ---- friend response models (used by FollowerResponse in Java) ----


@dataclass
class Person:
    xuid: str
    is_favorite: bool = False
    is_following_caller: bool = False
    is_followed_by_caller: bool = False
    is_identity_shared: bool = False
    added_date_time_utc: Optional[datetime] = None
    display_name: str = ""
    real_name: str = ""
    display_pic_raw: str = ""
    show_user_as_avatar: bool = False
    gamertag: str = ""
    gamer_score: str = ""
    modern_gamertag: str = ""
    modern_gamertag_suffix: str = ""
    unique_modern_gamertag: str = ""
    xbox_one_rep: str = ""
    presence_state: str = ""
    presence_text: str = ""
    is_broadcasting: bool = False
    is_quarantined: bool = False
    is_xbox360_gamerpic: bool = False
    last_seen_date_time_utc: Optional[datetime] = None
    linked_accounts: Optional[list[dict]] = None
    color_theme: str = ""
    preferred_flag: str = ""
    preferred_platforms: Optional[list[str]] = None

    @classmethod
    def from_json(cls, json_data: dict) -> "Person":
        out = cls(xuid=str(json_data.get("xuid", "")))
        for key in (
            "isFavorite",
            "isFollowingCaller",
            "isFollowedByCaller",
            "isIdentityShared",
            "showUserAsAvatar",
            "isBroadcasting",
            "isQuarantined",
            "isXbox360Gamerpic",
        ):
            attr = _snake(key)
            setattr(out, attr, bool(json_data.get(key, False)))
        for key in (
            "displayName",
            "realName",
            "displayPicRaw",
            "gamertag",
            "gamerScore",
            "modernGamertag",
            "modernGamertagSuffix",
            "uniqueModernGamertag",
            "xboxOneRep",
            "presenceState",
            "presenceText",
            "colorTheme",
            "preferredFlag",
        ):
            setattr(out, _snake(key), str(json_data.get(key, "") or ""))
        if json_data.get("addedDateTimeUtc"):
            out.added_date_time_utc = parse_xbox_datetime(json_data["addedDateTimeUtc"])
        if json_data.get("lastSeenDateTimeUtc"):
            out.last_seen_date_time_utc = parse_xbox_datetime(
                json_data["lastSeenDateTimeUtc"]
            )
        out.linked_accounts = json_data.get("linkedAccounts")
        out.preferred_platforms = json_data.get("preferredPlatforms")
        return out

    def merge(self, person: "Person") -> "Person":
        if person.added_date_time_utc is not None:
            self.added_date_time_utc = person.added_date_time_utc
        if person.is_followed_by_caller:
            self.is_followed_by_caller = True
        if person.is_following_caller:
            self.is_following_caller = True
        return self


@dataclass
class FollowerResponse:
    people: Optional[list[Person]] = None

    @classmethod
    def from_json(cls, json_data: dict) -> "FollowerResponse":
        people = None
        if json_data.get("people") is not None:
            people = [Person.from_json(p) for p in json_data["people"]]
        return cls(people=people)


@dataclass
class FriendModifyResponse:
    code: int
    description: str
    source: str

    @classmethod
    def from_json(cls, json_data: dict) -> "FriendModifyResponse":
        return cls(
            code=int(json_data.get("code", -1)),
            description=str(json_data.get("description", "") or ""),
            source=str(json_data.get("source", "") or ""),
        )


@dataclass
class FriendStatusResponse:
    xuid: str
    added_date_time_utc: Optional[datetime]
    is_favorite: bool
    is_followed_by_caller: bool
    is_following_caller: bool
    is_identity_shared: bool
    is_squad_mate_with: bool
    is_unfollowing_feed: bool

    @classmethod
    def from_json(cls, json_data: dict) -> "FriendStatusResponse":
        return cls(
            xuid=str(json_data.get("xuid", "")),
            added_date_time_utc=parse_xbox_datetime(json_data["addedDateTimeUtc"])
            if json_data.get("addedDateTimeUtc")
            else None,
            is_favorite=bool(json_data.get("isFavorite", False)),
            is_followed_by_caller=bool(json_data.get("isFollowedByCaller", False)),
            is_following_caller=bool(json_data.get("isFollowingCaller", False)),
            is_identity_shared=bool(json_data.get("isIdentityShared", False)),
            is_squad_mate_with=bool(json_data.get("isSquadMateWith", False)),
            is_unfollowing_feed=bool(json_data.get("isUnfollowingFeed", False)),
        )


@dataclass
class FriendRequestResponse:
    people: Optional[list[Person]] = None

    @classmethod
    def from_json(cls, json_data: dict) -> "FriendRequestResponse":
        people = None
        if json_data.get("people") is not None:
            people = [Person.from_json(p) for p in json_data["people"]]
        return cls(people=people)


@dataclass
class FriendRequestAcceptResponse:
    xuid: Optional[str]
    is_friend: bool

    @classmethod
    def from_json(cls, json_data: dict) -> "FriendRequestAcceptResponse":
        return cls(
            xuid=json_data.get("xuid"),
            is_friend=bool(json_data.get("isFriend", False)),
        )


def _snake(name: str) -> str:
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def parse_xbox_datetime(value: str) -> datetime:
    """Parse the variations of ISO 8601 used by Xbox Live.

    They can't decide on a format so we have to support all of them:
    yyyy-MM-dd'T'HH:mm:ss.SSSSSS / ...SSSSSSS / trailing Z.
    """
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1]
    if "." in text:
        head, frac = text.split(".", 1)
        frac = (frac + "000000")[:6]
        text = f"{head}.{frac}"
    else:
        text += ".000000"
    dt = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%f")
    return dt.replace(tzinfo=timezone.utc)
