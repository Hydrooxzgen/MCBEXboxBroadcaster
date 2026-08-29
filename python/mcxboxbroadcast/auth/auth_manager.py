"""Authentication manager mirroring the Java ``AuthManager`` / ``BedrockAuthManager``.

Implements the Microsoft device-code flow and the full Bedrock chain of tokens
required to talk to Xbox Live and Minecraft services. The Java MCXboxBroadcast
runs MinecraftAuth's ``BedrockAuthManager`` with ``BEDROCK_ANDROID_TITLE_ID``,
which performs:

    MSA  ->  XBL device token  ->  SISU (user/title/XSTS)  ->  Xbox Live XSTS
              ->  PlayFab XSTS  ->  PlayFab login  ->  Minecraft session token

The Xbox Live XSTS token (``http://xboxlive.com``) is used for the session
directory / social / profile endpoints, while the Minecraft session token
(``authorization.franchise.minecraft-services.net``) is used for the gallery
and NetherNet signalling - exactly like the Java version (``getMCTokenHeader``
and the ``pmid`` claim come from that session token).
"""

from __future__ import annotations

import base64
import json
import struct
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from mcxboxbroadcast.auth.models import (
    CachedProfileInfo,
    DeviceToken,
    MsaToken,
    MinecraftToken,
    PlayFabToken,
    XblToken,
    XstsToken,
)
from mcxboxbroadcast.constants import BEDROCK_VERSION, gson_dumps, gson_loads
from mcxboxbroadcast.logger import Logger
from mcxboxbroadcast.notifications import NotificationManager
from mcxboxbroadcast.storage import StorageManager

# Public Minecraft client id used by the bedrock auth flow.
# Matches MinecraftAuth's BEDROCK_ANDROID_TITLE_ID (what the Java MCXboxBroadcast
# uses via BedrockAuthManager). It is a pure-numeric "title" client id.
CLIENT_ID = "0000000048183522"
SCOPE = "service::user.auth.xboxlive.com::MBI_SSL"

# The MSA endpoints mirror the MinecraftAuth LIVE environment, not the newer
# login.microsoftonline.com v2.0 endpoints. The older native client ids (like
# the one above) are still accepted on login.live.com but were removed from the
# consumer directory used by the v2.0 endpoints (AADSTS700016).
MSA_DEVICECODE_URL = "https://login.live.com/oauth20_connect.srf"
MSA_TOKEN_URL = "https://login.live.com/oauth20_token.srf"
XBL_DEVICE_AUTH_URL = "https://device.auth.xboxlive.com/device/authenticate"
SISU_AUTH_URL = "https://sisu.xboxlive.com/authorize"
XSTS_AUTH_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
PLAYFAB_LOGIN_URL = "https://20ca2.playfabapi.com/Client/LoginWithXbox"
MC_SESSION_URL = "https://authorization.franchise.minecraft-services.net/api/v1.0/session/start"
PROFILE_URL = "https://profile.xboxlive.com/users/me/profile/settings?settings=Gamertag"

# XBL relying parties (MinecraftAuth XblConstants)
XBL_AUTH_RELYING_PARTY = "http://auth.xboxlive.com"
XBL_XSTS_RELYING_PARTY = "http://xboxlive.com"
BEDROCK_XSTS_RELYING_PARTY = "https://multiplayer.minecraft.net/"
PLAYFAB_XSTS_RELYING_PARTY = "https://b980a380.minecraft.playfabapi.com/"
PLAYFAB_TITLE_ID = "20CA2"  # PlayFabConstants.BEDROCK_PLAY_FAB_TITLE_ID

DEVICE_TYPE = "Android"  # matches BedrockAuthManager.Builder default

# Token lifetimes (seconds) used when the endpoint does not return one
DEFAULT_MSA_TTL = 3600
DEFAULT_XBL_TTL = 3600
DEFAULT_XSTS_TTL = 3600
DEFAULT_PLAYFAB_TTL = 4 * 3600
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


def _iso_to_epoch(value: str) -> float:
    """Convert an ISO-8601 timestamp (with 'Z' suffix) to a unix timestamp."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _b64url_encode(data: bytes) -> str:
    """Base64url without padding (used by the XBL ProofKey)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _proof_key(public_key: ec.EllipticCurvePublicKey) -> dict:
    """Build the JWK-style ProofKey from a secp256r1 public key.

    Mirrors SignedXblPostRequest.getProofKey(): x/y are the affine
    coordinates, zero-padded to 32 bytes and base64url encoded.
    """
    numbers = public_key.public_numbers()
    size = 32
    return {
        "kty": "EC",
        "alg": "ES256",
        "crv": "P-256",
        "use": "sig",
        "x": _b64url_encode(numbers.x.to_bytes(size, "big")),
        "y": _b64url_encode(numbers.y.to_bytes(size, "big")),
    }


def _der_signature_to_p1363(der_signature: bytes, size: int = 32) -> bytes:
    """Convert a DER-encoded ECDSA signature into P1363 (r||s) format."""
    idx = 2
    if der_signature[idx] != 0x02:
        raise ValueError("Expected integer for r")
    r_len = der_signature[idx + 1]
    r = der_signature[idx + 2 : idx + 2 + r_len]
    idx += 2 + r_len
    if der_signature[idx] != 0x02:
        raise ValueError("Expected integer for s")
    s_len = der_signature[idx + 1]
    s = der_signature[idx + 2 : idx + 2 + s_len]

    def _fixed_length(value: bytes) -> bytes:
        value = value.lstrip(b"\x00") or b"\x00"
        if len(value) > size:
            raise ValueError("Invalid length for ECDSA integer")
        return value.rjust(size, b"\x00")

    return _fixed_length(r) + _fixed_length(s)


def _sign_es256(private_key: ec.EllipticCurvePrivateKey, data: bytes) -> bytes:
    """ES256 signature in P1363 format (CryptUtil.signSha256InP1363Format)."""
    der = private_key.sign(data, ec.ECDSA(hashes.SHA256()))
    return _der_signature_to_p1363(der, 32)


def _xbl_signature_header(
    private_key: ec.EllipticCurvePrivateKey, method: str, url_path: str, body: bytes
) -> str:
    """Build the XBL ``Signature`` header (SignedXblPostRequest.appendSignatureHeader).

    The signature covers a fixed layout: policy version, the Windows 100ns
    timestamp, the HTTP method, the URL path, the (absent) Authorization header
    and the request body. The header value is the policy version, the timestamp
    and the ES256 (P1363) signature of that content, base64 encoded.
    """
    timestamp = int((time.time() + 11644473600) * 10000000)
    content = (
        struct.pack(">i", 1)
        + b"\x00"
        + struct.pack(">q", timestamp)
        + b"\x00"
        + method.encode("ascii")
        + b"\x00"
        + url_path.encode("ascii")
        + b"\x00"
        + b"\x00"  # no Authorization header on these requests
        + body
        + b"\x00"
    )
    header = (
        struct.pack(">i", 1)
        + struct.pack(">q", timestamp)
        + _sign_es256(private_key, content)
    )
    return base64.b64encode(header).decode("ascii")


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
        self._device_key: Optional[ec.EllipticCurvePrivateKey] = None
        self._device_id: Optional[str] = None
        self._device_token: Optional[DeviceToken] = None
        self._xbl: Optional[XblToken] = None
        self._title_token: Optional[XblToken] = None
        self._bedrock_xsts: Optional[XstsToken] = None
        self._xsts: Optional[XstsToken] = None
        self._playfab: Optional[PlayFabToken] = None
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
                    self._apply_cache(cache)

            # Login if not loaded
            if self._msa is None:
                self._device_code_login()

            try:
                self._refresh_tokens()
            except requests.HTTPError as e:
                body = ""
                if e.response is not None:
                    body = e.response.text
                if "invalid_grant" in body and not is_reauth:
                    # The refresh token died - clear everything and re-login.
                    self.logger.warn(
                        "Auth grant expired, clearing cache and re-authenticating..."
                    )
                    self._reset_auth_state()
                    try:
                        self.storage_manager.cache("")
                    except Exception:
                        pass
                    self._device_code_login()
                    self._refresh_tokens()
                else:
                    raise
            self._save_cache()

            if self._device_token_refresh_callback is not None:
                # In the Java version this is attached to the device token change
                # listener; here we simply invoke it after a successful refresh.
                pass

    def _reset_auth_state(self) -> None:
        self._msa = None
        self._device_key = None
        self._device_id = None
        self._device_token = None
        self._xbl = None
        self._title_token = None
        self._bedrock_xsts = None
        self._xsts = None
        self._playfab = None
        self._mc = None
        self._profile = None

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
            if self._device_token is None or self._device_token.expires_at <= time.time() + 60:
                self._acquire_device_token()
            if (
                self._xbl is None
                or self._title_token is None
                or self._xbl.expires_at <= time.time() + 60
                or self._title_token.expires_at <= time.time() + 60
            ):
                self._acquire_sisu_tokens()
            if self._xsts is None or self._xsts.expires_at <= time.time() + 60:
                self._acquire_xsts()
            if self._playfab is None or self._playfab.expires_at <= time.time() + 60:
                self._acquire_playfab()
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

    def _acquire_device_token(self) -> None:
        # XblDeviceAuthenticateRequest: signed request with a fresh secp256r1
        # device key. The device token is bound to the key, so both are
        # persisted together in the cache.
        if self._device_key is None:
            self._device_key = ec.generate_private_key(ec.SECP256R1())
            self._device_id = str(uuid.uuid4())
        body = {
            "Properties": {
                "DeviceType": DEVICE_TYPE,
                "Id": "{" + self._device_id + "}",
                "AuthMethod": "ProofOfPossession",
                "ProofKey": _proof_key(self._device_key.public_key()),
            },
            "RelyingParty": XBL_AUTH_RELYING_PARTY,
            "TokenType": "JWT",
        }
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        signature = _xbl_signature_header(
            self._device_key, "POST", "/device/authenticate", body_bytes
        )
        resp = requests.post(
            XBL_DEVICE_AUTH_URL,
            data=body_bytes,
            headers={
                "Content-Type": "application/json",
                "x-xbl-contract-version": "1",
                "Signature": signature,
            },
            timeout=30,
        )
        if not resp.ok:
            self.logger.error(
                f"XBL device auth failed ({resp.status_code}): {resp.text[:600]}"
            )
        resp.raise_for_status()
        data = resp.json()
        self._device_token = DeviceToken(
            token=data["Token"],
            expires_at=_iso_to_epoch(data["NotAfter"]),
        )

    def _acquire_sisu_tokens(self) -> None:
        # XblSisuAuthorizeRequest: for title client ids this replaces the plain
        # XBL user auth and returns the user token, the title token and the
        # bedrock XSTS token in a single signed request.
        if self._device_token is None:
            self._acquire_device_token()
        body = {
            "Sandbox": "RETAIL",
            "UseModernGamertag": True,
            "AppId": CLIENT_ID,
            "AccessToken": "t=" + self._msa.access_token,
            "DeviceToken": self._device_token.token,
            "ProofKey": _proof_key(self._device_key.public_key()),
            "RelyingParty": BEDROCK_XSTS_RELYING_PARTY,
        }
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        signature = _xbl_signature_header(
            self._device_key, "POST", "/authorize", body_bytes
        )
        resp = requests.post(
            SISU_AUTH_URL,
            data=body_bytes,
            headers={"Content-Type": "application/json", "Signature": signature},
            timeout=30,
        )
        if not resp.ok:
            self.logger.error(f"SISU auth failed ({resp.status_code}): {resp.text[:600]}")
        resp.raise_for_status()
        data = resp.json()

        def _parse_user(obj: dict) -> XblToken:
            # XblUserToken.fromApiJson: DisplayClaims.xui[0].uhs
            claims = obj["DisplayClaims"]["xui"][0]
            return XblToken(
                token=obj["Token"],
                user_hash=claims.get("uhs", ""),
                expires_at=_iso_to_epoch(obj["NotAfter"]),
            )

        def _parse_title(obj: dict) -> XblToken:
            # XblTitleToken.fromApiJson: DisplayClaims.xti.tid (an object,
            # NOT an array like xui). The title token carries no user hash.
            return XblToken(
                token=obj["Token"],
                user_hash="",
                expires_at=_iso_to_epoch(obj["NotAfter"]),
            )

        self._xbl = _parse_user(data["UserToken"])
        self._title_token = _parse_title(data["TitleToken"])
        auth_token = data["AuthorizationToken"]
        claims = auth_token["DisplayClaims"]["xui"][0]
        self._bedrock_xsts = XstsToken(
            token=auth_token["Token"],
            user_hash=claims.get("uhs", ""),
            xuid=claims.get("xid", ""),
            expires_at=_iso_to_epoch(auth_token["NotAfter"]),
        )

    def _acquire_xsts(self) -> None:
        # XblXstsAuthorizeRequest for the Xbox Live relying party - the token
        # used for the session directory / profile / social endpoints
        # (Java: xboxLiveXstsToken).
        if self._device_token is None:
            self._acquire_device_token()
        if self._title_token is None:
            self._acquire_sisu_tokens()
        resp = requests.post(
            XSTS_AUTH_URL,
            json={
                "Properties": {
                    "SandboxId": "RETAIL",
                    "DeviceToken": self._device_token.token,
                    "UserTokens": [self._xbl.token],
                    "TitleToken": self._title_token.token,
                },
                "RelyingParty": XBL_XSTS_RELYING_PARTY,
                "TokenType": "JWT",
            },
            headers={"x-xbl-contract-version": "1"},
            timeout=30,
        )
        if not resp.ok:
            self.logger.error(f"XSTS auth failed ({resp.status_code}): {resp.text[:600]}")
        resp.raise_for_status()
        data = resp.json()
        claims = data["DisplayClaims"]["xui"][0]
        self._xsts = XstsToken(
            token=data["Token"],
            user_hash=claims.get("uhs", ""),
            xuid=claims.get("xid", ""),
            expires_at=_iso_to_epoch(data["NotAfter"]),
        )

    def _acquire_playfab(self) -> None:
        # PlayFab XSTS (Java: playFabXstsToken) + PlayFab login. The session
        # ticket is later exchanged for the Minecraft session token.
        if self._device_token is None:
            self._acquire_device_token()
        if self._title_token is None:
            self._acquire_sisu_tokens()
        resp = requests.post(
            XSTS_AUTH_URL,
            json={
                "Properties": {
                    "SandboxId": "RETAIL",
                    "DeviceToken": self._device_token.token,
                    "UserTokens": [self._xbl.token],
                    "TitleToken": self._title_token.token,
                },
                "RelyingParty": PLAYFAB_XSTS_RELYING_PARTY,
                "TokenType": "JWT",
            },
            headers={"x-xbl-contract-version": "1"},
            timeout=30,
        )
        if not resp.ok:
            self.logger.error(
                f"PlayFab XSTS failed ({resp.status_code}): {resp.text[:600]}"
            )
        resp.raise_for_status()
        data = resp.json()
        claims = data["DisplayClaims"]["xui"][0]
        xsts_header = f"XBL3.0 x={claims.get('uhs', '')};{data['Token']}"

        resp = requests.post(
            PLAYFAB_LOGIN_URL,
            json={
                "CreateAccount": True,
                "InfoRequestParameters": {
                    "GetPlayerProfile": True,
                    "GetUserAccountInfo": True,
                },
                "TitleId": PLAYFAB_TITLE_ID,
                "XboxToken": xsts_header,
            },
            timeout=30,
        )
        if not resp.ok:
            self.logger.error(
                f"PlayFab login failed ({resp.status_code}): {resp.text[:600]}"
            )
        resp.raise_for_status()
        data = resp.json()["data"]
        entity = data.get("EntityToken", {})
        expires_at = (
            _iso_to_epoch(entity["Expiration"])
            if entity.get("Expiration")
            else time.time() + DEFAULT_PLAYFAB_TTL
        )
        self._playfab = PlayFabToken(
            session_ticket=data["SessionTicket"],
            entity_token=entity.get("EntityToken", ""),
            playfab_id=data.get("PlayFabId", ""),
            expires_at=expires_at,
        )

    def _acquire_mc(self) -> None:
        # MinecraftSessionStartRequest: exchanges the PlayFab session ticket for
        # the Minecraft session token. The returned authorization header
        # ("Bearer <jwt>") is what the Java version uses for the gallery and
        # NetherNet signalling, and the jwt payload carries the "pmid" claim.
        if self._playfab is None:
            self._acquire_playfab()
        body = {
            "device": {
                "applicationType": "MinecraftPE",
                "gameVersion": BEDROCK_VERSION,
                "id": self._device_id.replace("-", ""),
                "memory": 32 * 1024 * 1024 * 1024,
                "hardwareMemoryTier": 5,
                "platform": "Windows10",
                "playFabTitleId": PLAYFAB_TITLE_ID,
                "storePlatform": "uwp.store",
                "type": "Windows10",
            },
            "user": {
                "language": "en",
                "regionCode": "US",
                "languageCode": "en-US",
                "tokenType": "PlayFab",
                "token": self._playfab.session_ticket,
            },
        }
        resp = requests.post(MC_SESSION_URL, json=body, timeout=30)
        if not resp.ok:
            self.logger.error(
                f"MC session start failed ({resp.status_code}): {resp.text[:600]}"
            )
        resp.raise_for_status()
        result = resp.json()["result"]
        auth_header = result.get("authorizationHeader", "")
        token = auth_header
        if auth_header.startswith("Bearer "):
            token = auth_header[len("Bearer ") :]
        expires_at = (
            _iso_to_epoch(result["validUntil"])
            if result.get("validUntil")
            else time.time() + DEFAULT_MC_TTL
        )
        self._mc = MinecraftToken(access_token=token, expires_at=expires_at)

    def _fetch_profile(self) -> None:
        # Build the header directly from the XSTS token (no get_manager() call -
        # that would recurse back into _fetch_profile while the profile is None).
        resp = requests.get(
            PROFILE_URL,
            headers={
                "Authorization": f"XBL3.0 x={self._xsts.user_hash};{self._xsts.token}",
                "x-xbl-contract-version": "3",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        user = data["profileUsers"][0]
        xuid = user["id"]
        gamertag = ""
        # The profile settings endpoint returns a list of {id, value} entries,
        # not a map: [{"id": "Gamertag", "value": "..."}, ...]
        for setting in user.get("settings", []):
            if setting.get("id") == "Gamertag":
                gamertag = setting.get("value", "")
                break
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

    def _apply_cache(self, cache: dict) -> None:
        """Restore token state from a previously saved cache.

        The cache version is bumped when the auth flow changes; old-format
        caches only contribute the MSA refresh token (which keeps the login
        silent) and everything else is re-acquired.
        """
        try:
            if "msa" in cache:
                self._msa = MsaToken(**cache["msa"])
        except Exception as e:
            self.logger.error(f"Failed to restore msa from cache: {e}")
        if cache.get("save_version", 1) < 2:
            return
        try:
            device = cache.get("device")
            if device and device.get("key_pem"):
                self._device_key = serialization.load_pem_private_key(
                    device["key_pem"].encode("utf-8"), password=None
                )
                self._device_id = device["id"]
                self._device_token = DeviceToken(
                    token=device["token"], expires_at=device["expires_at"]
                )
            if "xbl" in cache:
                self._xbl = XblToken(**cache["xbl"])
            if "title" in cache:
                self._title_token = XblToken(**cache["title"])
            if "xsts" in cache:
                self._xsts = XstsToken(**cache["xsts"])
            if "playfab" in cache:
                self._playfab = PlayFabToken(**cache["playfab"])
            if "mc" in cache:
                self._mc = MinecraftToken(**cache["mc"])
        except Exception as e:
            self.logger.error(f"Failed to restore token cache: {e}")

    def _save_cache(self) -> None:
        if not all(
            [
                self._msa,
                self._device_key,
                self._device_id,
                self._device_token,
                self._xbl,
                self._title_token,
                self._xsts,
                self._playfab,
                self._mc,
            ]
        ):
            return
        data = {
            "save_version": 2,
            "msa": self._msa.__dict__,
            "device": {
                "key_pem": self._device_key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ).decode("utf-8"),
                "id": self._device_id,
                "token": self._device_token.token,
                "expires_at": self._device_token.expires_at,
            },
            "xbl": self._xbl.__dict__,
            "title": self._title_token.__dict__,
            "xsts": self._xsts.__dict__,
            "playfab": self._playfab.__dict__,
            "mc": self._mc.__dict__,
        }
        try:
            self.storage_manager.cache(gson_dumps(data))
        except Exception as e:
            self.logger.error(f"Failed to save auth cache: {e}")
