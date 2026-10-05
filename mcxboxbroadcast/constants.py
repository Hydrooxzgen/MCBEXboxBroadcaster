"""Constants for MCXboxBroadcast, ported from Java Constants.java."""

from __future__ import annotations

SERVICE_CONFIG_ID = "4fc10100-5f7a-4470-899b-280835760c07"  # Minecraft service config ID
TEMPLATE_NAME = "MinecraftLobby"
TITLE_ID = "896928775"  # Minecraft Windows Edition title ID

CREATE_SESSION = (
    "https://sessiondirectory.xboxlive.com/serviceconfigs/"
    + SERVICE_CONFIG_ID
    + "/sessionTemplates/"
    + TEMPLATE_NAME
    + "/sessions/%s"
)
JOIN_SESSION = "https://sessiondirectory.xboxlive.com/handles/%s/session"

RTA_WEBSOCKET = "wss://rta.xboxlive.com/connect"
CREATE_HANDLE = "https://sessiondirectory.xboxlive.com/handles"

PEOPLE = "https://social.xboxlive.com/users/me/people/xuid(%s)"
USER_PRESENCE = "https://userpresence.xboxlive.com/users/xuid(%s)/devices/current/titles/current"
FOLLOWERS = "https://peoplehub.xboxlive.com/users/me/people/followers"
SOCIAL = "https://peoplehub.xboxlive.com/users/me/people/social"
FRIENDS = "https://peoplehub.xboxlive.com/users/me/people/friends"
FRIEND_REQUESTS = "https://peoplehub.xboxlive.com/users/me/people/friendrequests(received)"
SOCIAL_SUMMARY = "https://social.xboxlive.com/users/me/summary"
FRIEND = "https://social.xboxlive.com/users/me/people/friends/v2/xuid(%s)"
FOLLOWER = "https://social.xboxlive.com/users/me/people/follower/xuid(%s)"

GALLERY = "https://persona.franchise.minecraft-services.net/api/v1.0/gallery"

WEBSOCKET_CONNECTION_TIMEOUT = 10.0  # seconds

# Gathered from scraped web requests, seems to use the below enum
# https://github.com/LiteLDev/LeviLamina/blob/main/src/mc/network/ConnectionType.h
CONNECTION_TYPE_JSON_RPC = 7

# Maximum friends count supported by Xbox Live
MAX_FRIENDS = 1000

# Bedrock protocol targeted by the micro nethernet server (1.26.50 / protocol 2193)
PROTOCOL_VERSION = 2193
MINECRAFT_VERSION = "1.26.50"

# Config version for upgrade purposes
CONFIG_VERSION = 5
