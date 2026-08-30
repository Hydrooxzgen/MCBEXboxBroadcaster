"""JSON models mirroring the Java ``models`` subpackages.

The dataclasses keep their camelCase field names so that serialising them
produces exactly the JSON payloads expected by the Xbox Live endpoints.
"""

from .session import (
    Connection,
    CreateHandleRequest,
    CreateHandleResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    FollowerResponse,
    JoinSessionRequest,
    MemberConstantsSystem,
    MemberPropertiesSystem,
    MemberSubscription,
    SessionCustomProperties,
    SessionMember,
    SessionProperties,
    SessionRef,
    SessionSystemProperties,
    SocialSummaryResponse,
)
from .friend import (
    FriendModifyResponse,
    FriendRequestAcceptResponse,
    FriendRequestResponse,
    FriendStatusResponse,
)
from .gallery import GalleryImage, GalleryResponse, GalleryUploadResponse
from .ws import MessageType

__all__ = [
    "Connection",
    "CreateHandleRequest",
    "CreateHandleResponse",
    "CreateSessionRequest",
    "CreateSessionResponse",
    "FollowerResponse",
    "JoinSessionRequest",
    "MemberConstantsSystem",
    "MemberPropertiesSystem",
    "MemberSubscription",
    "SessionCustomProperties",
    "SessionMember",
    "SessionProperties",
    "SessionRef",
    "SessionSystemProperties",
    "SocialSummaryResponse",
    "FriendModifyResponse",
    "FriendRequestAcceptResponse",
    "FriendRequestResponse",
    "FriendStatusResponse",
    "GalleryImage",
    "GalleryResponse",
    "GalleryUploadResponse",
    "MessageType",
]
