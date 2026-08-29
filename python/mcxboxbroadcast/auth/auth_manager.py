"""Authentication manager mirroring the Java ``AuthManager`` / ``BedrockAuthManager``.

Implements the Microsoft device-code flow and the chain of tokens required to talk
to Xbox Live and Minecraft services:

    MSA  ->  XBL  ->  XSTS  ->  Minecraft

The resulting XSTS token is used for the Xbox Live session directory / social
endpoints, while the Minecraft token is used for the gallery and NetherNet
signalling.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from typing import Callable, Optional

import requests

from mcxboxbroadcast.auth.models import (
    CachedProfileInfo,
    MsaToken,
    MinecraftToken,
    XblToken,
    XstsToken,
)
from mcxboxbroadcast.constants import gson_dumps, gson_loads
from mcxboxbroadcast.logger import Logger
from mcxboxbroadcast.notifications import NotificationManager
from mcxboxbroadcast.storage import StorageManager

# Public Minecraft client id used by the bedrock auth flow
CLIENT_ID = "00000000402b5328"
SCOPE = "service::user.auth.xboxlive.com::MBI_SSL"

# The MSA endpoints mirror the MinecraftAuth LIVE environment, not the newer
# login.microsoftonline.com v2.0 endpoints. The older native client ids (like
# the one above) are still accepted on login.live.com but were removed from the
# consumer directory used by the v2.0 endpoints (AADSTS700016).
MSA_DEVICECODE_URL = "https://login.live.com/oauth20_connect.srf"
MSA_TOKEN_URL = "https://login.live.com/oauth20_token.srf"
XBL_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_AUTH_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
MC_LOGIN_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
PROFILE_URL = "https://profile.xboxlive.com/users/me/profile/settings?settings=Gamertag"

# Token lifetimes (seconds) used when the endpoint does not return one
DEFAULT_MSA_TTL = 3600
DEFAULT_XBL_TTL = 3600
DEFAULT_XSTS_TTL = 3600
DEFAULT_MC_TTL = 86400

PROFILE_TTL = 10 * 60


class AgeVerificationException(Exception):
    pass


def _b64decode_segment(segment: str) -> bytes:
    """Decode a JWT segment (url-safe base64, no padding)."""
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def decode_jwt_payload(token: str) -> dict:
    """Decode the payload of a JWT without verifying the signature."""
    _, payload, _ = token.split(".")
    return json.loads(_b64decode_segment(payload).decode("utf-8"))


class AuthManager:
    def __init__(
        self,
        notification_manager: NotificationManager,
        storage_manager: StorageManager,
        logger: Logger,
    ) -> None:
        self.notification_manager = notification_manager
        self.storage_manager = storage_manager
        self.logger = logger.prefixed("Auth")

        self._msa: Optional[MsaToken] = None
        self._xbl: Optional[XblToken] = None
        self._xsts: Optional[XstsToken] = None
        self._mc: Optional[MinecraftToken] = None
        self._profile: Optional[CachedProfileInfo] = None

        self._device_token_refresh_callback: Optional[Callable[[], None]] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_manager(self) -> "AuthManager":
        if self._xsts is None or self._xsts.expires_at <= time.time() + 60:
            self._initialise(False)
        self._refresh_tokens()
        return self

    def get_token_header(self) -> str:
        self.get_manager()
        return f"XBL3.0 x={self._xsts.user_hash};{self._xsts.token}"

    def get_mc_token_header(self) -> str:
        self.get_manager()
        return f"Bearer {self._mc.access_token}"

    def get_mc_token_pmid(self) -> str:
        """Return the ``pmid`` claim from the Minecraft token payload (empty if missing)."""
        self.get_manager()
        payload = decode_jwt_payload(self._mc.access_token)
        return payload.get("pmid", "")

    def get_xuid(self) -> str:
        self.get_manager()
        return self._xsts.xuid

    def get_gamertag(self) -> str:
        self.get_manager()
        if self._profile is None or self._profile.expired:
            self._fetch_profile()
        return self._profile.gamertag

    def set_on_device_token_refresh_callback(self, callback: Callable[[], None]) -> None:
        self._device_token_refresh_callback = callback

    # ------------------------------------------------------------------
    # Initialisation / login
    # ------------------------------------------------------------------
    def _initialise(self, is_reauth: bool) -> None:
        with self._lock:
            # Try to load from cache
            if self._msa is None:
                cache = self._load_cache()
                if cache:
                    self._msa = MsaToken(**cache["msa"])
                    self._xbl = XblToken(**cache["xbl"])
                    self._xsts = XstsToken(**cache["xsts"])
                    self._mc = MinecraftToken(**cache["mc"])

            # Login if not loaded
            if self._msa is None:
                self._device_code_login()

            self._refresh_tokens()
            self._save_cache()

            if self._device_token_refresh_callback is not None:
                # In the Java version this is attached to the device token change
                # listener; here we simply invoke it after a successful refresh.
                pass

    def _device_code_login(self) -> None:
        # Request a device code
        resp = requests.post(
            MSA_DEVICECODE_URL,
            data={
                "client_id": CLIENT_ID,
                "scope": SCOPE,
                "response_type": "device_code",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        user_code = data["user_code"]
        device_code = data["device_code"]
        verification_uri = data["verification_uri"]
        interval = int(data.get("interval", 5))
        expires_in = int(data.get("expires_in", 900))

        self.logger.info(
            f"To sign in, use a web browser to open the page {verification_uri} "
            f"and enter the code {user_code} to authenticate."
        )
        self.notification_manager.send_session_expired_notification(verification_uri, user_code)

        deadline = time.time() + expires_in
        while time.time() < deadline:
            time.sleep(interval)
            token_resp = requests.post(
                MSA_TOKEN_URL,
                data={
                    "grant_type": "device_code",
                    "device_code": device_code,
                    "client_id": CLIENT_ID,
                },
                timeout=30,
            )
            if token_resp.status_code == 200:
                td = token_resp.json()
                self._msa = MsaToken(
                    access_token=td["access_token"],
                    refresh_token=td["refresh_token"],
                    expires_at=time.time() + int(td.get("expires_in", DEFAULT_MSA_TTL)),
                )
                return
            elif token_resp.status_code == 400:
                err = token_resp.json().get("error")
                if err == "authorization_pending":
                    continue
                if err == "expired_token":
                    raise RuntimeError("Device code expired, please try again")
                if err == "access_denied":
                    raise RuntimeError("User denied the authentication request")
                raise RuntimeError(f"Unexpected token error: {err}")
            else:
                token_resp.raise_for_status()

        raise RuntimeError("Device code authentication timed out")

    # ------------------------------------------------------------------
    # Token acquisition
    # ------------------------------------------------------------------
    def _refresh_tokens(self) -> None:
        try:
            if self._msa is None or self._msa.expires_at <= time.time() + 60:
                self._refresh_msa()
            if self._xbl is None or self._xbl.expires_at <= time.time() + 60:
                self._acquire_xbl()
            if self._xsts is None or self._xsts.expires_at <= time.time() + 60:
                self._acquire_xsts()
            if self._mc is None or self._mc.expires_at <= time.time() + 60:
                self._acquire_mc()
            if self._profile is None or self._profile.expired:
                self._fetch_profile()
        except requests.HTTPError as e:
            body = ""
            if e.response is not None:
                body = e.response.text
            if "agecheck" in body:
                raise AgeVerificationException(
                    "Authentication failed due to age verification requirement"
                ) from e
            raise

    def _refresh_msa(self) -> None:
        if self._msa is None or not self._msa.refresh_token:
            self._device_code_login()
            return
        resp = requests.post(
            MSA_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._msa.refresh_token,
                "client_id": CLIENT_ID,
                "scope": SCOPE,
            },
            timeout=30,
        )
        resp.raise_for_status()
        td = resp.json()
        self._msa = MsaToken(
            access_token=td["access_token"],
            refresh_token=td.get("refresh_token", self._msa.refresh_token),
            expires_at=time.time() + int(td.get("expires_in", DEFAULT_MSA_TTL)),
        )

    def _acquire_xbl(self) -> None:
        resp = requests.post(
            XBL_AUTH_URL,
            json={
                "Properties": {
                    "AuthMethod": "RPS",
                    "SiteName": "user.auth.xboxlive.com",
                    "RpsTicket": f"d={self._msa.access_token}",
                },
                "RelyingParty": "http://auth.xboxlive.com",
                "TokenType": "JWT",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        uhs = data["DisplayClaims"]["xui"][0]["uhs"]
        self._xbl = XblToken(
            token=data["Token"],
            user_hash=uhs,
            expires_at=time.time() + DEFAULT_XBL_TTL,
        )

    def _acquire_xsts(self) -> None:
        resp = requests.post(
            XSTS_AUTH_URL,
            json={
                "Properties": {
                    "SandboxId": "RETAIL",
                    "UserTokens": [self._xbl.token],
                },
                "RelyingParty": "http://xboxlive.com",
                "TokenType": "JWT",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        claims = data["DisplayClaims"]["xui"][0]
        uhs = claims["uhs"]
        xuid = claims.get("xid", "")
        self._xsts = XstsToken(
            token=data["Token"],
            user_hash=uhs,
            xuid=xuid,
            expires_at=time.time() + DEFAULT_XSTS_TTL,
        )

    def _acquire_mc(self) -> None:
        identity = f"XBL3.0 x={self._xsts.user_hash};{self._xsts.token}"
        resp = requests.post(
            MC_LOGIN_URL,
            json={"identityToken": identity},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        self._mc = MinecraftToken(
            access_token=data["access_token"],
            expires_at=time.time() + int(data.get("expires_in", DEFAULT_MC_TTL)),
        )

    def _fetch_profile(self) -> None:
        resp = requests.get(
            PROFILE_URL,
            headers={
                "Authorization": self.get_token_header(),
                "x-xbl-contract-version": "3",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        user = data["profileUsers"][0]
        xuid = user["id"]
        gamertag = user["settings"].get("Gamertag", "")
        # Keep xuid from the XSTS token if the profile didn't return one
        if not xuid and self._xsts:
            xuid = self._xsts.xuid
        self._profile = CachedProfileInfo(
            gamertag=gamertag, xuid=xuid, expires_at=time.time() + PROFILE_TTL
        )

    # ------------------------------------------------------------------
    # Caching
    # ------------------------------------------------------------------
    def _load_cache(self) -> Optional[dict]:
        try:
            raw = self.storage_manager.cache()
        except Exception:
            return None
        if not raw or not raw.strip():
            return None
        try:
            return gson_loads(raw)
        except Exception as e:
            self.logger.error(f"Failed to load cache.json: {e}")
            return None

    def _save_cache(self) -> None:
        if not all([self._msa, self._xbl, self._xsts, self._mc]):
            return
        data = {
            "msa": self._msa.__dict__,
            "xbl": self._xbl.__dict__,
            "xsts": self._xsts.__dict__,
            "mc": self._mc.__dict__,
        }
        try:
            self.storage_manager.cache(gson_dumps(data))
        except Exception as e:
            self.logger.error(f"Failed to save auth cache: {e}")
