"""Low-level Xbox Live / MSA / PlayFab / Minecraft services requests.

Each request class mirrors the equivalent in the MinecraftAuth library
(see PORTING_NOTES.md for the protocol notes).
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import requests

from ..logger import Logger
from .models import (
    CachedProfileInfo,
    MinecraftMultiplayerToken,
    MinecraftSession,
    MsaToken,
    PlayFabToken,
    XblDeviceToken,
    XblTitleToken,
    XblUserToken,
    XblXstsToken,
)

logger = Logger("Auth")

MSA_CLIENT_ID = "0000000048183522"  # Bedrock Android title id
MSA_SCOPE = "service::user.auth.xboxlive.com::MBI_SSL"
# The MinecraftAuth library defaults to the legacy LIVE environment for title ids
MSA_DEVICE_CODE_URL = "https://login.live.com/oauth20_connect.srf"
MSA_TOKEN_URL = "https://login.live.com/oauth20_token.srf"

XBL_RELYING_PARTY = "http://auth.xboxlive.com"
XBL_XSTS_RELYING_PARTY = "http://xboxlive.com"
BEDROCK_XSTS_RELYING_PARTY = "https://multiplayer.minecraft.net/"
BEDROCK_PLAY_FAB_XSTS_RELYING_PARTY = "https://b980a380.minecraft.playfabapi.com/"

DEVICE_AUTH_URL = "https://device.auth.xboxlive.com/device/authenticate"
SISU_AUTHORIZE_URL = "https://sisu.xboxlive.com/authorize"
XSTS_AUTHORIZE_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
PROFILE_URL = "https://profile.xboxlive.com/users/me/profile/settings?settings=Gamertag"

PLAY_FAB_TITLE_ID = "20CA2"
PLAY_FAB_LOGIN_URL = f"https://{PLAY_FAB_TITLE_ID.lower()}.playfabapi.com/Client/LoginWithXbox"

MC_SESSION_START_URL = "https://authorization.franchise.minecraft-services.net/api/v1.0/session/start"
MC_MULTIPLAYER_SESSION_START_URL = (
    "https://authorization.franchise.minecraft-services.net/api/v1.0/multiplayer/session/start"
)

USER_AGENT = "MCXboxBroadcast-Python"


class MsaDeviceCode:
    def __init__(self, json_data: dict) -> None:
        self.expires_at = time.time() + int(json_data["expires_in"])
        self.interval = int(json_data.get("interval", 5))
        self.device_code = json_data["device_code"]
        self.user_code = json_data["user_code"]
        self.verification_uri = json_data["verification_uri"]


class AuthRequestException(Exception):
    def __init__(self, message: str, status: int = 0, body: str = "", error: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.error = error


def _raise_for_status(response: requests.Response, context: str) -> dict:
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code != 200:
        # Xbox/MSA errors often carry a human readable message
        message = data.get("Message") or data.get("error_description") or data.get("message")
        if not message and isinstance(data.get("error"), dict):
            message = data["error"].get("message")
        if not message:
            message = f"{context} failed with status {response.status_code}: {response.text[:300]}"
        raise AuthRequestException(message, response.status_code, response.text[:2000])
    return data


def request_msa_device_code(session: requests.Session) -> MsaDeviceCode:
    response = session.post(
        MSA_DEVICE_CODE_URL,
        data={
            "client_id": MSA_CLIENT_ID,
            "scope": MSA_SCOPE,
            "response_type": "device_code",
        },
        timeout=15,
    )
    return MsaDeviceCode(_raise_for_status(response, "MSA device code"))


def request_msa_device_code_token(session: requests.Session, device_code: str) -> MsaToken:
    response = session.post(
        MSA_TOKEN_URL,
        data={"client_id": MSA_CLIENT_ID, "grant_type": "device_code", "device_code": device_code},
        timeout=15,
    )
    if response.status_code != 200:
        error = data.get("error", "")
        if error == "authorization_pending":
            raise AuthRequestException("authorization_pending", 400, error=error)
        if error == "slow_down":
            raise AuthRequestException("slow_down", 400, error=error)
        raise AuthRequestException(
            data.get("error_description") or error or "MSA token request failed",
            response.status_code,
            response.text[:2000],
            error=error,
        )
    data = response.json()
    return MsaToken(
        expire_time_ms=(time.time() + int(data["expires_in"])) * 1000,
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
    )


def request_msa_refresh_token(session: requests.Session, refresh_token: str) -> MsaToken:
    response = session.post(
        MSA_TOKEN_URL,
        data={
            "client_id": MSA_CLIENT_ID,
            "scope": MSA_SCOPE,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=15,
    )
    data = _raise_for_status(response, "MSA refresh")
    return MsaToken(
        expire_time_ms=(time.time() + int(data["expires_in"])) * 1000,
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
    )


def request_device_token(
    session: requests.Session,
    device_type: str,
    device_id: str,
    proof_key: dict,
    sign_header: str,
) -> XblDeviceToken:
    body = {
        "Properties": {
            "DeviceType": device_type,
            "Id": "{" + device_id + "}",
            "AuthMethod": "ProofOfPossession",
            "ProofKey": proof_key,
        },
        "RelyingParty": XBL_RELYING_PARTY,
        "TokenType": "JWT",
    }
    response = session.post(
        DEVICE_AUTH_URL,
        json=body,
        headers={
            "x-xbl-contract-version": "1",
            "Signature": sign_header,
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json; charset=utf-8",
        },
        timeout=15,
    )
    data = _raise_for_status(response, "Device token")
    return XblDeviceToken(
        expire_time_ms=_xbox_not_after(data["NotAfter"]),
        token=data["Token"],
        did=data["DisplayClaims"]["xdi"]["did"],
    )


def request_sisu_tokens(
    session: requests.Session,
    access_token: str,
    device_token: str,
    proof_key: dict,
    sign_header: str,
    relying_party: str = BEDROCK_XSTS_RELYING_PARTY,
) -> tuple[XblUserToken, XblTitleToken, XblXstsToken]:
    body = {
        "Sandbox": "RETAIL",
        "UseModernGamertag": True,
        "AppId": MSA_CLIENT_ID,
        "AccessToken": "t=" + access_token,
        "DeviceToken": device_token,
        "ProofKey": proof_key,
        "RelyingParty": relying_party,
    }
    response = session.post(
        SISU_AUTHORIZE_URL,
        json=body,
        headers={
            "Signature": sign_header,
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json; charset=utf-8",
        },
        timeout=15,
    )
    data = _raise_for_status(response, "SISU authorize")
    user = data["UserToken"]
    title = data["TitleToken"]
    xsts = data["AuthorizationToken"]
    return (
        XblUserToken(
            expire_time_ms=_xbox_not_after(user["NotAfter"]),
            token=user["Token"],
            uhs=user["DisplayClaims"]["xui"][0]["uhs"],
        ),
        XblTitleToken(
            expire_time_ms=_xbox_not_after(title["NotAfter"]), token=title["Token"]
        ),
        _xsts_from_json(xsts),
    )


def request_xsts_token(
    session: requests.Session,
    device_token: str,
    user_token: str,
    title_token: Optional[str],
    relying_party: str,
) -> XblXstsToken:
    properties: dict = {
        "SandboxId": "RETAIL",
        "DeviceToken": device_token,
        "UserTokens": [user_token],
    }
    if title_token is not None:
        properties["TitleToken"] = title_token
    body = {
        "Properties": properties,
        "RelyingParty": relying_party,
        "TokenType": "JWT",
    }
    response = session.post(
        XSTS_AUTHORIZE_URL,
        json=body,
        headers={"x-xbl-contract-version": "1", "User-Agent": USER_AGENT},
        timeout=15,
    )
    data = _raise_for_status(response, "XSTS authorize")
    return _xsts_from_json(data)


def _xsts_from_json(data: dict) -> XblXstsToken:
    return XblXstsToken(
        expire_time_ms=_xbox_not_after(data["NotAfter"]),
        token=data["Token"],
        user_hash=data["DisplayClaims"]["xui"][0]["uhs"],
    )


def request_profile(session: requests.Session, xsts_token: XblXstsToken) -> CachedProfileInfo:
    response = session.get(
        PROFILE_URL,
        headers={
            "Authorization": xsts_token.authorization_header,
            "x-xbl-contract-version": "3",
            "User-Agent": USER_AGENT,
        },
        timeout=15,
    )
    data = _raise_for_status(response, "Profile")
    users = data.get("profileUsers") or []
    if not users:
        raise AuthRequestException("Profile response contained no users")
    settings = {s["id"]: s["value"] for s in users[0].get("settings", [])}
    return CachedProfileInfo(
        gamertag=settings.get("Gamertag", ""),
        xuid=str(users[0].get("id", "")),
    )


def request_play_fab_login(session: requests.Session, xsts_token: XblXstsToken) -> PlayFabToken:
    body = {
        "CreateAccount": True,
        "InfoRequestParameters": {
            "GetPlayerProfile": True,
            "GetUserAccountInfo": True,
        },
        "TitleId": PLAY_FAB_TITLE_ID,
        "XboxToken": xsts_token.authorization_header,
    }
    response = session.post(PLAY_FAB_LOGIN_URL, json=body, timeout=15)
    data = _raise_for_status(response, "PlayFab login")
    inner = data.get("data", {})
    return PlayFabToken(
        expire_time_ms=time.time() * 1000 + 1000 * 60 * 60,  # PlayFab tickets are long lived; refresh with the XSTS token
        play_fab_id=inner.get("PlayFabId", ""),
        session_ticket=inner.get("SessionTicket", ""),
    )


def request_minecraft_session(
    session: requests.Session,
    play_fab_token: PlayFabToken,
    game_version: str,
    device_id: str,
) -> MinecraftSession:
    body = {
        "device": {
            "applicationType": "MinecraftPE",
            "gameVersion": game_version,
            "id": device_id.replace("-", ""),
            "memory": 32 * 1024 * 1024 * 1024,
            "hardwareMemoryTier": 5,
            "platform": "Windows10",
            "playFabTitleId": PLAY_FAB_TITLE_ID,
            "storePlatform": "uwp.store",
            "type": "Windows10",
        },
        "user": {
            "language": "en",
            "regionCode": "US",
            "languageCode": "en-US",
            "tokenType": "PlayFab",
            "token": play_fab_token.session_ticket,
        },
    }
    response = session.post(MC_SESSION_START_URL, json=body, timeout=15)
    data = _raise_for_status(response, "Minecraft session")
    result = data.get("result", {})
    return MinecraftSession(
        expire_time_ms=_iso_to_ms(result.get("validUntil")),
        authorization_header=result.get("authorizationHeader", ""),
    )


def request_minecraft_multiplayer_token(
    session: requests.Session,
    minecraft_session: MinecraftSession,
    public_key_b64: str,
) -> MinecraftMultiplayerToken:
    response = session.post(
        MC_MULTIPLAYER_SESSION_START_URL,
        json={"publicKey": public_key_b64},
        headers={
            "Authorization": minecraft_session.authorization_header,
            "User-Agent": USER_AGENT,
        },
        timeout=15,
    )
    data = _raise_for_status(response, "Minecraft multiplayer token")
    result = data.get("result", {})
    return MinecraftMultiplayerToken(
        expire_time_ms=_iso_to_ms(result.get("validUntil")),
        signed_token=result.get("signedToken", ""),
    )


def _xbox_not_after(not_after: str) -> float:
    return _iso_to_ms(not_after)


def _iso_to_ms(value: Optional[str]) -> float:
    if not value:
        return (time.time() + 3600) * 1000
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1]
    if "." in text:
        head, frac = text.split(".", 1)
        frac = (frac + "000000")[:6]
        text = f"{head}.{frac}"
    else:
        text += ".000000"
    dt = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000
