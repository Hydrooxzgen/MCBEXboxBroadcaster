"""Friend management models, mirroring ``core/models/friend``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class FriendRequestAcceptResponse:
    xuid: Optional[str] = None
    addedDateTimeUtc: Optional[str] = None
    isFriend: bool = False


@dataclass
class FriendRequestResponse:
    accountLinkDetails: Any = None
    friendFinderState: Any = None
    friendRequestSummary: Any = None
    recommendationSummary: Any = None
    people: Optional[list] = None  # list[FollowerResponse.Person]


@dataclass
class FriendModifyResponse:
    code: int = 0
    description: Optional[str] = None
    source: Optional[str] = None
    traceInformation: Any = None


@dataclass
class FriendStatusResponse:
    xuid: Optional[str] = None
    addedDateTimeUtc: Any = None
    isFavorite: bool = False
    socialNetworks: Any = None
    isFollowedByCaller: bool = False
    isFollowingCaller: bool = False
    isIdentityShared: bool = False
    isSquadMateWith: bool = False
    isUnfollowingFeed: bool = False


__all__ = [
    "FriendRequestAcceptResponse",
    "FriendRequestResponse",
    "FriendModifyResponse",
    "FriendStatusResponse",
]
