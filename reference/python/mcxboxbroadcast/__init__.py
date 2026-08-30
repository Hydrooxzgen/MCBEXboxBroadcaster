"""MCXboxBroadcast - broadcast a Geyser/Bedrock server over Xbox Live.

Python port of the Java MCXboxBroadcast project.
"""

from mcxboxbroadcast.constants import (
    BEDROCK_PROTOCOL_VERSION,
    BEDROCK_VERSION,
    MAX_FRIENDS,
    gson_dumps,
    gson_loads,
)
from mcxboxbroadcast.exceptions import (
    AgeVerificationException,
    SessionCreationException,
    SessionUpdateException,
    XboxFriendsException,
)
from mcxboxbroadcast.logger import Logger, setup_logging
from mcxboxbroadcast.session_info import ExpandedSessionInfo, SessionInfo
from mcxboxbroadcast.session_manager import SessionManager
from mcxboxbroadcast.session_manager_core import SessionManagerCore
from mcxboxbroadcast.sub_session_manager import SubSessionManager

__version__ = "1.0.0"

__all__ = [
    "BEDROCK_PROTOCOL_VERSION",
    "BEDROCK_VERSION",
    "MAX_FRIENDS",
    "gson_dumps",
    "gson_loads",
    "AgeVerificationException",
    "SessionCreationException",
    "SessionUpdateException",
    "XboxFriendsException",
    "Logger",
    "setup_logging",
    "ExpandedSessionInfo",
    "SessionInfo",
    "SessionManager",
    "SessionManagerCore",
    "SubSessionManager",
]
