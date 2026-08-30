"""Mojang keys used for Bedrock login verification."""

import threading
import time

# SubjectPublicKeyInfo (DER) of Mojang's root EC P-384 key, base64 encoded.
# Used by the legacy certificate-chain login path.
MOJANG_ROOT_PUBLIC_KEY_B64 = (
    "MHYwEAYHKoZIzj0CAQYFK4EEACIDYgAECRXueJeTDqNRRgJi/vlRufByu/2G0i2Ebt6YMar5QX/"
    "R0DIIyrJMcUpruK4QveTfJSTp3Shlq4Gk34cD/4GUWwkv0DVuzeuB+tXija7HBxii03NHDbPAD"
    "0AKnLr2wdAp"
)

DISCOVERY_ENDPOINT = (
    "https://client.discovery.minecraft-services.net/api/v1.0/discovery/"
    "MinecraftPE/builds/1.0.0.0"
)

_jwks_lock = threading.Lock()
_jwks_cache = None  # (fetch_time, {kid: rsa_public_key}, issuer)


def _fetch_json(url: str) -> dict:
    import requests

    response = requests.get(url, headers={"Accept": "application/json"}, timeout=10)
    response.raise_for_status()
    return response.json()


def _load_jwks() -> tuple[dict, str]:
    global _jwks_cache
    with _jwks_lock:
        if _jwks_cache is not None and time.time() - _jwks_cache[0] < 3600:
            return _jwks_cache[1], _jwks_cache[2]

        discovery = _fetch_json(DISCOVERY_ENDPOINT)
        service_uri = discovery["result"]["serviceEnvironments"]["auth"]["prod"]["serviceUri"]
        openid = _fetch_json(service_uri + "/.well-known/openid-configuration")
        jwks_uri = openid["jwks_uri"]
        issuer = openid["issuer"]

        jwks = _fetch_json(jwks_uri)
        from cryptography.hazmat.primitives.asymmetric import rsa

        keys = {}
        for key in jwks.get("keys", []):
            if key.get("kty") != "RSA":
                continue
            n = int.from_bytes(_b64url_decode(key["n"]), "big")
            e = int.from_bytes(_b64url_decode(key["e"]), "big")
            keys[key.get("kid", "")] = rsa.RSAPublicNumbers(e, n).public_key()
        if not keys:
            raise RuntimeError("Mojang JWKS contained no RSA keys")
        _jwks_cache = (time.time(), keys, issuer)
        return keys, issuer


def _b64url_decode(text: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verify_mojang_token(token: str) -> dict:
    """Verify a Mojang RS256 login token against the JWKS and return its claims.

    Raises on invalid signature or missing identity claims.
    """
    import json

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    header_b64, payload_b64, signature_b64 = token.split(".")
    header = json.loads(_b64url_decode(header_b64))
    if header.get("alg") != "RS256":
        raise RuntimeError(f"Unexpected login token alg: {header.get('alg')}")
    kid = header.get("kid")

    keys, _issuer = _load_jwks()
    key = keys.get(kid) or next(iter(keys.values()))

    signing_input = f"{header_b64}.{payload_b64}".encode()
    try:
        key.verify(_b64url_decode(signature_b64), signing_input, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as ex:
        raise RuntimeError("Login token signature is invalid") from ex

    claims = json.loads(_b64url_decode(payload_b64))
    if not claims.get("xid") or not claims.get("xname"):
        raise RuntimeError(
            f"Login token is missing identity claims (has: {sorted(claims.keys())})"
        )
    return claims
