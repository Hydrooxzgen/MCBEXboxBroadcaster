"""Bedrock auth chain manager, the Python counterpart of MinecraftAuth's
BedrockAuthManager (title-client SISU flow) wrapped by Java AuthManager.

Handles: MSA device-code login, XBL device token, SISU tokens, XSTS tokens
for the Xbox Live and PlayFab relying parties, PlayFab login, the Minecraft
session + multiplayer token, and persisting everything to cache.json.
"""

from __future__ import annotations

import json
import threading
import uuid
from typing import Callable, Optional

import requests

from .. import constants
from ..exceptions import AgeVerificationException
from ..logger import Logger
from ..storage.storage_manager import StorageManager
from ..notifications.notification_manager import NotificationManager
from . import crypto, xbl_requests as req
from .models import (
    CachedProfileInfo,
    Holder,
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

SAVE_VERSION = 1


class BedrockAuthManager:
    def __init__(self, session: requests.Session, game_version: str):
        self._http = session
        self.game_version = game_version

        self.device_type = "Android"
        self.device_id = str(uuid.uuid4())
        from cryptography.hazmat.primitives.asymmetric import ec

        self._device_private_key = ec.generate_private_key(ec.SECP256R1())
        self._device_public_key = self._device_private_key.public_key()
        self._session_private_key = ec.generate_private_key(ec.SECP384R1())
        self._session_public_key = self._session_private_key.public_key()

        self._sisu_lock = threading.RLock()

        self.msa_token = Holder[MsaToken](self._refresh_msa_token)
        self.xbl_device_token = Holder[XblDeviceToken](self._refresh_device_token)
        self.xbl_user_token = Holder[XblUserToken](self._refresh_user_token, self._sisu_lock)
        self.xbl_title_token = Holder[XblTitleToken](self._refresh_title_token, self._sisu_lock)
        self.bedrock_xsts_token = Holder[XblXstsToken](self._refresh_bedrock_xsts, self._sisu_lock)
        self.play_fab_xsts_token = Holder[XblXstsToken](self._refresh_play_fab_xsts)
        self.xbox_live_xsts_token = Holder[XblXstsToken](self._refresh_xbox_live_xsts)
        self.profile_info = Holder[CachedProfileInfo](self._refresh_profile)
        self.play_fab_token = Holder[PlayFabToken](self._refresh_play_fab_token)
        self.minecraft_session = Holder[MinecraftSession](self._refresh_minecraft_session)
        self.minecraft_multiplayer_token = Holder[MinecraftMultiplayerToken](
            self._refresh_multiplayer_token
        )

        self.change_listeners: list[Callable[[], None]] = []
        for holder in self._all_holders():
            holder.change_listeners.append(lambda _v: self._on_change())

    # -- construction / persistence ------------------------------------
    @classmethod
    def from_json(cls, session: requests.Session, game_version: str, data: dict) -> "BedrockAuthManager":
        manager = cls.__new__(cls)
        manager._init_from_json(session, game_version, data)
        return manager

    def _init_from_json(self, session: requests.Session, game_version: str, data: dict) -> None:
        from cryptography.hazmat.primitives.asymmetric import ec

        BedrockAuthManager._lazy_init(self, session, game_version)
        self.device_id = data.get("deviceId") or str(uuid.uuid4())
        self.device_type = data.get("deviceType", "Android")
        if data.get("devicePrivateKeyPem"):
            self._device_private_key = crypto.private_key_from_pem(data["devicePrivateKeyPem"])
            self._device_public_key = self._device_private_key.public_key()
        if data.get("sessionPrivateKeyPem"):
            self._session_private_key = crypto.private_key_from_pem(data["sessionPrivateKeyPem"])
            self._session_public_key = self._session_private_key.public_key()

        msa = data.get("msaToken")
        if msa:
            self.msa_token.set(
                MsaToken(
                    expire_time_ms=msa.get("expire_time_ms", msa.get("expireTimeMs", 0)),
                    access_token=msa.get("access_token", msa.get("accessToken", "")),
                    refresh_token=msa.get("refresh_token", msa.get("refreshToken")),
                ),
                msa.get("expire_time_ms", msa.get("expireTimeMs", 0)),
            )
        token_map = {
            "xblDeviceToken": (self.xbl_device_token, XblDeviceToken),
            "xblUserToken": (self.xbl_user_token, XblUserToken),
            "xblTitleToken": (self.xbl_title_token, XblTitleToken),
            "bedrockXstsToken": (self.bedrock_xsts_token, XblXstsToken),
            "playFabXstsToken": (self.play_fab_xsts_token, XblXstsToken),
            "xboxLiveXstsToken": (self.xbox_live_xsts_token, XblXstsToken),
        }
        for key, (holder, cls_) in token_map.items():
            stored = dict(data.get(key) or {})
            if stored:
                expire = stored.pop("expireTimeMs", None)
                if expire is None:
                    expire = stored.pop("expire_time_ms", 0)
                holder.set(cls_(expire_time_ms=expire, **stored), expire)
        stored_profile = dict(data.get("profileInfo") or {})
        if stored_profile:
            expires = stored_profile.pop("expiresAtMs", None)
            if expires is None:
                expires = stored_profile.pop("expires_at_ms", 0)
            self.profile_info.set(CachedProfileInfo(**stored_profile), expires / 1000.0 if expires > 1e11 else expires)
        stored_playfab = dict(data.get("playFabToken") or {})
        if stored_playfab:
            expire = stored_playfab.pop("expireTimeMs", None)
            if expire is None:
                expire = stored_playfab.pop("expire_time_ms", 0)
            self.play_fab_token.set(PlayFabToken(expire_time_ms=expire, **stored_playfab), expire)
        stored_session = dict(data.get("minecraftSession") or {})
        if stored_session:
            expire = stored_session.pop("expireTimeMs", None)
            if expire is None:
                expire = stored_session.pop("expire_time_ms", 0)
            self.minecraft_session.set(
                MinecraftSession(expire_time_ms=expire, **stored_session), expire
            )
        stored_mp = dict(data.get("minecraftMultiplayerToken") or {})
        if stored_mp:
            expire = stored_mp.pop("expireTimeMs", None)
            if expire is None:
                expire = stored_mp.pop("expire_time_ms", 0)
            self.minecraft_multiplayer_token.set(
                MinecraftMultiplayerToken(expire_time_ms=expire, **stored_mp), expire
            )

    @staticmethod
    def _lazy_init(manager: "BedrockAuthManager", session: requests.Session, game_version: str) -> None:
        # Reuse __init__ via a fresh instance and copy state
        fresh = BedrockAuthManager.__new__(BedrockAuthManager)
        BedrockAuthManager.__init__(fresh, session, game_version)
        manager.__dict__.update(fresh.__dict__)

    def to_json(self) -> dict:
        out = {
            "_saveVersion": SAVE_VERSION,
            "deviceType": self.device_type,
            "deviceId": self.device_id,
            "devicePrivateKeyPem": crypto.private_key_to_pem(self._device_private_key),
            "sessionPrivateKeyPem": crypto.private_key_to_pem(self._session_private_key),
        }
        if self.msa_token.has_value:
            t = self.msa_token.get_cached()
            out["msaToken"] = {
                "expire_time_ms": t.expire_time_ms,
                "access_token": t.access_token,
                "refresh_token": t.refresh_token,
            }
        if self.xbl_device_token.has_value:
            t = self.xbl_device_token.get_cached()
            out["xblDeviceToken"] = {
                "expire_time_ms": t.expire_time_ms,
                "token": t.token,
                "did": t.did,
            }
        if self.xbl_user_token.has_value:
            t = self.xbl_user_token.get_cached()
            out["xblUserToken"] = {
                "expire_time_ms": t.expire_time_ms,
                "token": t.token,
                "uhs": t.uhs,
            }
        if self.xbl_title_token.has_value:
            t = self.xbl_title_token.get_cached()
            out["xblTitleToken"] = {"expire_time_ms": t.expire_time_ms, "token": t.token}
        if self.bedrock_xsts_token.has_value:
            t = self.bedrock_xsts_token.get_cached()
            out["bedrockXstsToken"] = {
                "expire_time_ms": t.expire_time_ms,
                "token": t.token,
                "user_hash": t.user_hash,
            }
        if self.play_fab_xsts_token.has_value:
            t = self.play_fab_xsts_token.get_cached()
            out["playFabXstsToken"] = {
                "expire_time_ms": t.expire_time_ms,
                "token": t.token,
                "user_hash": t.user_hash,
            }
        if self.xbox_live_xsts_token.has_value:
            t = self.xbox_live_xsts_token.get_cached()
            out["xboxLiveXstsToken"] = {
                "expire_time_ms": t.expire_time_ms,
                "token": t.token,
                "user_hash": t.user_hash,
            }
        if self.profile_info.has_value:
            t = self.profile_info.get_cached()
            out["profileInfo"] = {
                "gamertag": t.gamertag,
                "xuid": t.xuid,
                "expires_at_ms": t.expires_at * 1000,
            }
        if self.play_fab_token.has_value:
            t = self.play_fab_token.get_cached()
            out["playFabToken"] = {
                "expire_time_ms": t.expire_time_ms,
                "play_fab_id": t.play_fab_id,
                "session_ticket": t.session_ticket,
            }
        if self.minecraft_session.has_value:
            t = self.minecraft_session.get_cached()
            out["minecraftSession"] = {
                "expire_time_ms": t.expire_time_ms,
                "authorization_header": t.authorization_header,
            }
        if self.minecraft_multiplayer_token.has_value:
            t = self.minecraft_multiplayer_token.get_cached()
            out["minecraftMultiplayerToken"] = {
                "expire_time_ms": t.expire_time_ms,
                "signed_token": t.signed_token,
            }
        return out

    def _all_holders(self):
        return (
            self.msa_token,
            self.xbl_device_token,
            self.xbl_user_token,
            self.xbl_title_token,
            self.bedrock_xsts_token,
            self.play_fab_xsts_token,
            self.xbox_live_xsts_token,
            self.profile_info,
            self.play_fab_token,
            self.minecraft_session,
            self.minecraft_multiplayer_token,
        )

    def _on_change(self) -> None:
        for listener in list(self.change_listeners):
            try:
                listener()
            except Exception:
                pass

    # -- refreshers (called by Holders) ---------------------------------
    def _refresh_msa_token(self) -> MsaToken:
        cached = self.msa_token.get_cached()
        if cached is None or cached.refresh_token is None:
            raise RuntimeError(
                "Can't refresh MSA token, because it was created without a refresh token. "
                "The user has to sign in again."
            )
        return req.request_msa_refresh_token(self._http, cached.refresh_token)

    def _proof_key(self) -> dict:
        return crypto.build_proof_key_jwk(self._device_public_key)

    def _sign(self, method: str, url: str, body: bytes) -> str:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        return crypto.build_signature_header(
            self._device_private_key, method, parsed.path + (f"?{parsed.query}" if parsed.query else ""), None, body
        )

    def _refresh_device_token(self) -> XblDeviceToken:
        logger.verbose("[auth] 1/6 Requesting Xbox device token...")
        url = req.DEVICE_AUTH_URL
        proof_key = self._proof_key()
        body = {
            "Properties": {
                "DeviceType": self.device_type,
                "Id": "{" + self.device_id + "}",
                "AuthMethod": "ProofOfPossession",
                "ProofKey": proof_key,
            },
            "RelyingParty": req.XBL_RELYING_PARTY,
            "TokenType": "JWT",
        }
        raw = json.dumps(body, separators=(",", ":")).encode()
        signature = self._sign("POST", url, raw)
        response = self._http.post(
            url,
            data=raw,
            headers={
                "x-xbl-contract-version": "1",
                "Signature": signature,
                "User-Agent": req.USER_AGENT,
                "Content-Type": "application/json; charset=utf-8",
            },
            timeout=15,
        )
        data = req._raise_for_status(response, "Device token")
        return XblDeviceToken(
            expire_time_ms=req._xbox_not_after(data["NotAfter"]),
            token=data["Token"],
            did=data["DisplayClaims"]["xdi"]["did"],
        )

    def _refresh_sisu_tokens(self) -> None:
        logger.verbose("[auth] 2/6 Requesting SISU tokens (user/title/XSTS)...")
        with self._sisu_lock:
            device_token = self.xbl_device_token.get_up_to_date()
            msa_token = self.msa_token.get_up_to_date()
            url = req.SISU_AUTHORIZE_URL
            body = {
                "Sandbox": "RETAIL",
                "UseModernGamertag": True,
                "AppId": req.MSA_CLIENT_ID,
                "AccessToken": "t=" + msa_token.access_token,
                "DeviceToken": device_token.token,
                "ProofKey": self._proof_key(),
                "RelyingParty": req.BEDROCK_XSTS_RELYING_PARTY,
            }
            raw = json.dumps(body, separators=(",", ":")).encode()
            signature = self._sign("POST", url, raw)
            response = self._http.post(
                url,
                data=raw,
                headers={
                    "Signature": signature,
                    "User-Agent": req.USER_AGENT,
                    "Content-Type": "application/json; charset=utf-8",
                },
                timeout=15,
            )
            data = req._raise_for_status(response, "SISU authorize")
            user = data["UserToken"]
            title = data["TitleToken"]
            xsts = data["AuthorizationToken"]
            self.xbl_user_token.set(
                XblUserToken(
                    expire_time_ms=req._xbox_not_after(user["NotAfter"]),
                    token=user["Token"],
                    uhs=user["DisplayClaims"]["xui"][0]["uhs"],
                ),
                req._xbox_not_after(user["NotAfter"]),
            )
            self.xbl_title_token.set(
                XblTitleToken(
                    expire_time_ms=req._xbox_not_after(title["NotAfter"]),
                    token=title["Token"],
                ),
                req._xbox_not_after(title["NotAfter"]),
            )
            self.bedrock_xsts_token.set(
                self._xsts_from_json(xsts), xsts["NotAfter"] and req._xbox_not_after(xsts["NotAfter"])
            )

    @staticmethod
    def _xsts_from_json(data: dict) -> XblXstsToken:
        return XblXstsToken(
            expire_time_ms=req._xbox_not_after(data["NotAfter"]),
            token=data["Token"],
            user_hash=data["DisplayClaims"]["xui"][0]["uhs"],
        )

    def _refresh_user_token(self) -> XblUserToken:
        self._refresh_sisu_tokens()
        return self.xbl_user_token.get_cached()

    def _refresh_title_token(self) -> XblTitleToken:
        self._refresh_sisu_tokens()
        return self.xbl_title_token.get_cached()

    def _refresh_bedrock_xsts(self) -> XblXstsToken:
        self._refresh_sisu_tokens()
        return self.bedrock_xsts_token.get_cached()

    def _refresh_play_fab_xsts(self) -> XblXstsToken:
        device_token = self.xbl_device_token.get_up_to_date()
        user_token = self.xbl_user_token.get_up_to_date()
        title_token = self.xbl_title_token.get_up_to_date()
        return req.request_xsts_token(
            self._http,
            device_token.token,
            user_token.token,
            title_token.token,
            req.BEDROCK_PLAY_FAB_XSTS_RELYING_PARTY,
        )

    def _refresh_xbox_live_xsts(self) -> XblXstsToken:
        logger.verbose("[auth] 3/6 Requesting Xbox Live XSTS token...")
        device_token = self.xbl_device_token.get_up_to_date()
        user_token = self.xbl_user_token.get_up_to_date()
        title_token = self.xbl_title_token.get_up_to_date()
        return req.request_xsts_token(
            self._http,
            device_token.token,
            user_token.token,
            title_token.token,
            req.XBL_XSTS_RELYING_PARTY,
        )

    def _refresh_profile(self) -> CachedProfileInfo:
        logger.verbose("[auth] 6/6 Fetching Xbox profile...")
        return req.request_profile(self._http, self.xbox_live_xsts_token.get_up_to_date())

    def _refresh_play_fab_token(self) -> PlayFabToken:
        logger.verbose("[auth] 4/6 Logging in to PlayFab...")
        return req.request_play_fab_login(self._http, self.play_fab_xsts_token.get_up_to_date())

    def _refresh_minecraft_session(self) -> MinecraftSession:
        logger.verbose("[auth] 5/6 Starting Minecraft session...")
        return req.request_minecraft_session(
            self._http,
            self.play_fab_token.get_up_to_date(),
            self.game_version,
            self.device_id,
        )

    def _refresh_multiplayer_token(self) -> MinecraftMultiplayerToken:
        return req.request_minecraft_multiplayer_token(
            self._http,
            self.minecraft_session.get_up_to_date(),
            crypto.encode_public_key_spki_b64(self._session_public_key),
        )

    # -- public API (mirrors Java AuthManager) ---------------------------
    def get_authorization_header(self) -> str:
        return self.xbox_live_xsts_token.get_up_to_date().authorization_header

    def get_play_fab_session_ticket(self) -> Optional[str]:
        try:
            return self.play_fab_token.get_up_to_date().session_ticket
        except Exception as ex:
            logger.error("Failed to get PlayFab session ticket", ex)
            return None

    def get_mc_token_header(self) -> str:
        return self.minecraft_session.get_up_to_date().authorization_header

    def get_pmsg_id(self) -> Optional[str]:
        """Extract the 'pmid' claim from the Minecraft session token JWT.

        The Java code parses the MCToken JWT carried by the Minecraft session
        authorization header ("MCToken <jwt>"), not the multiplayer token.
        """
        import base64

        header_value = self.minecraft_session.get_up_to_date().authorization_header
        try:
            jwt_token = header_value.split(" ", 2)[1] if " " in header_value else header_value
            payload_b64 = jwt_token.split(".")[1]
            padding = "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
            pmid = payload.get("pmid")
            if pmid is None:
                logger.warn(
                    f"pmid claim missing from Minecraft session token payload keys: "
                    f"{sorted(payload.keys())}"
                )
            return pmid
        except Exception as ex:
            logger.warn(f"Failed to parse pmid from Minecraft session token: {ex}")
            return None

    def get_gamertag(self) -> str:
        return self.profile_info.get_cached().gamertag

    def get_xuid(self) -> str:
        return self.profile_info.get_cached().xuid


class AuthManager:
    """The Java-side AuthManager wrapper: cache handling + device-code login UX."""

    def __init__(
        self,
        notification_manager: NotificationManager,
        storage_manager: StorageManager,
        logger_: Logger,
    ) -> None:
        self.notification_manager = notification_manager
        self.storage_manager = storage_manager
        self.logger = logger_.prefixed("Auth")

        self.auth_manager: Optional[BedrockAuthManager] = None
        self.on_device_token_refresh_callback: Optional[Callable[[], None]] = None
        self._http = requests.Session()
        self._login_lock = threading.Lock()

    def initialise(self, is_reauth: bool = False) -> None:
        with self._login_lock:
            try:
                # Try to load the cached auth data
                if self.auth_manager is None:
                    cache_data = self.storage_manager.cache()
                    if cache_data.strip():
                        try:
                            json_data = json.loads(cache_data)
                            self.auth_manager = BedrockAuthManager.from_json(
                                self._http, constants.MINECRAFT_VERSION, json_data
                            )
                        except Exception as ex:
                            self.logger.error("Failed to load cache.json", ex)

                # Device-code login if we still have no auth manager
                if self.auth_manager is None:
                    self.auth_manager = self._login_with_device_code()

                self._refresh_tokens()
                self.auth_manager.change_listeners.append(self.save_to_cache)
                self.save_to_cache()

                if self.on_device_token_refresh_callback is not None:
                    self.auth_manager.xbl_device_token.change_listeners.append(
                        lambda _v: self.on_device_token_refresh_callback()
                    )
            except AgeVerificationException:
                # Handled elsewhere, do not log
                return
            except Exception as ex:
                message = str(ex)
                if "invalid_grant" in message or "Can't refresh MSA token" in message:
                    if is_reauth:
                        self.logger.error(
                            "Re-auth still failed with invalid_grant. Sign in with username "
                            "and password, not a passwordless method.",
                            ex,
                        )
                        return
                    self.logger.warn("Auth grant expired, clearing cache and re-authenticating...")
                    try:
                        self.storage_manager.cache("")
                    except IOError:
                        pass
                    self.auth_manager = None
                    self.initialise(True)
                    return
                self.logger.error("Failed to get/refresh auth token", ex)

    def _login_with_device_code(self) -> BedrockAuthManager:
        import time as _time

        device_code = None
        last_log_time = 0.0
        poll_count = 0
        token = None

        while token is None:
            # Request a fresh device code when none exists or the current one expired
            if device_code is None or _time.time() >= device_code.expires_at:
                device_code = req.request_msa_device_code(self._http)
                poll_count = 0
                self.logger.info("=" * 60)
                self.logger.info("需要登录 Xbox 账号 / Microsoft sign-in required:")
                self.logger.info(f"  1. Open this page:  {device_code.verification_uri}")
                self.logger.info(f"  2. Enter this code: {device_code.user_code}")
                self.logger.info(
                    "  3. Sign in with the account to broadcast, then approve."
                )
                self.logger.info("等待你在浏览器中完成登录... / Waiting for sign-in...")
                self.logger.info("=" * 60)
                self.notification_manager.send_session_expired_notification(
                    device_code.verification_uri, device_code.user_code
                )

            try:
                token = req.request_msa_device_code_token(self._http, device_code.device_code)
                break
            except req.AuthRequestException as ex:
                if ex.error == "authorization_pending":
                    poll_count += 1
                    now = _time.time()
                    # Show a liveness message roughly every 30 seconds
                    if now - last_log_time >= 30:
                        last_log_time = now
                        self.logger.info(
                            "仍在等待浏览器授权... / Still waiting for you to finish "
                            "signing in on the website (open the link, enter the code, "
                            "choose the account and approve)."
                        )
                    _time.sleep(device_code.interval)
                    continue
                if ex.error == "slow_down":
                    _time.sleep(device_code.interval + 5)
                    continue
                if ex.error == "expired_token":
                    device_code = None  # request a new code
                    continue
                raise

        self.logger.info("登录成功！/ Sign-in successful, continuing authentication chain...")
        manager = BedrockAuthManager(self._http, constants.MINECRAFT_VERSION)
        manager.msa_token.set(token, token.expire_time_ms)
        return manager

    def _refresh_tokens(self) -> None:
        if self.auth_manager is None:
            raise RuntimeError("Not authenticated")
        try:
            self.auth_manager.xbox_live_xsts_token.get_up_to_date()
            self.auth_manager.play_fab_token.get_up_to_date()
            self._refresh_profile_info()
        except req.AuthRequestException as ex:
            if "agecheck" in str(ex) or ex.status == 403 and "age" in str(ex).lower():
                raise AgeVerificationException(
                    "Authentication failed due to age verification requirement", ex
                )
            raise

    def _refresh_profile_info(self) -> None:
        holder = self.auth_manager.profile_info
        cached = holder.get_cached()
        import time as _time

        if cached is None or _time.time() >= cached.expires_at:
            fresh = req.request_profile(
                self._http, self.auth_manager.xbox_live_xsts_token.get_up_to_date()
            )
            holder.set(fresh, fresh.expires_at * 1000)

    def save_to_cache(self) -> None:
        try:
            if self.auth_manager is not None:
                self.storage_manager.cache(json.dumps(self.auth_manager.to_json()))
        except Exception as ex:
            self.logger.error("Failed to save auth cache", ex)

    def get_manager(self) -> BedrockAuthManager:
        if self.auth_manager is None:
            self.initialise(False)
        try:
            self._refresh_tokens()
        except AgeVerificationException:
            # Propagate so callers can show the age verification message
            raise
        except Exception as ex:
            self.logger.error("Failed to refresh tokens", ex)
            self.initialise(False)
        if self.auth_manager is None:
            raise RuntimeError(
                "Authentication failed. See the error above; fix the problem "
                "and restart the program."
            )
        return self.auth_manager

    def set_on_device_token_refresh_callback(self, callback: Callable[[], None]) -> None:
        self.on_device_token_refresh_callback = callback
        if self.auth_manager is not None:
            self.auth_manager.xbl_device_token.change_listeners.append(
                lambda _v: callback()
            )

    def get_gamertag(self) -> str:
        return self.get_manager().get_gamertag()

    def get_xuid(self) -> str:
        return self.get_manager().get_xuid()
