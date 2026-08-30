"""Xbox Live request signing (ECDSA-P256 "Signature" header) and proof keys.

Ported from MinecraftAuth's SignedXblPostRequest / CryptUtil, see
PORTING_NOTES.md section 1 for the wire format.
"""

from __future__ import annotations

import base64
import time
from typing import Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.exceptions import InvalidSignature

WINDOWS_EPOCH_DELTA = 11644473600  # seconds between 1601-01-01 and 1970-01-01


def generate_ec_key_pair(curve: ec.EllipticCurve):
    private_key = ec.generate_private_key(curve)
    return private_key, private_key.public_key()


def encode_public_key_spki_b64(public_key) -> str:
    """Standard DER SubjectPublicKeyInfo, base64 encoded (used for ProofKey
    uploads to Minecraft services)."""
    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return base64.b64encode(der).decode()


def private_key_to_pem(private_key) -> str:
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def public_key_to_pem(public_key) -> str:
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def private_key_from_pem(pem: str):
    return serialization.load_pem_private_key(pem.encode(), password=None)


def public_key_from_pem(pem: str):
    return serialization.load_pem_public_key(pem.encode())


def build_proof_key_jwk(public_key) -> dict:
    """JWK (EC P-256) representation of the proof key public part."""
    numbers = public_key.public_numbers()
    size = (public_key.curve.key_size + 7) // 8
    return {
        "kty": "EC",
        "alg": "ES256",
        "crv": "P-256",
        "use": "sig",
        "x": _b64url(numbers.x.to_bytes(size, "big")),
        "y": _b64url(numbers.y.to_bytes(size, "big")),
    }


def build_signature_header(
    private_key,
    method: str,
    url_path_and_query: str,
    authorization_header: Optional[str],
    body: bytes,
) -> str:
    """Build the base64 "Signature" header proving possession of the device key."""
    windows_timestamp = (int(time.time()) + WINDOWS_EPOCH_DELTA) * 10_000_000

    content = bytearray()
    content += _int32_be(1)  # policy version
    content += b"\x00"
    content += _int64_be(windows_timestamp)
    content += b"\x00"
    content += method.encode()
    content += b"\x00"
    content += url_path_and_query.encode()
    content += b"\x00"
    if authorization_header:
        content += authorization_header.encode()
    content += b"\x00"
    content += body
    content += b"\x00"

    signature = private_key.sign(bytes(content), ec.ECDSA(hashes.SHA256()))
    # Convert DER signature to raw P1363 (r||s) format
    r, s = _der_to_p1363(signature, private_key.curve.key_size)

    header_data = bytearray()
    header_data += _int32_be(1)
    header_data += _int64_be(windows_timestamp)
    header_data += r + s
    return base64.b64encode(bytes(header_data)).decode()


def verify_es256_jws(token: str, public_key_der_or_pem) -> bool:
    """Verify an ES256 compact JWS (used for the Minecraft login chain)."""
    import json as _json

    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
        signing_input = f"{header_b64}.{payload_b64}".encode()
        header = _json.loads(_b64url_decode(header_b64))
        if header.get("alg") != "ES256":
            return False
        der_signature = _p1363_to_der(_b64url_decode(signature_b64))
        if isinstance(public_key_der_or_pem, bytes):
            key = serialization.load_der_public_key(public_key_der_or_pem)
        else:
            key = public_key_der_or_pem
        key.verify(der_signature, signing_input, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, KeyError):
        return False


def verify_jws(token: str, public_key_der: bytes) -> tuple[bool, str]:
    """Algorithm-adaptive compact JWS verification.

    Supports ES256/ES384/ES512 and RS256/384/512. Returns (ok, alg).
    """
    import json as _json

    from cryptography.hazmat.primitives.asymmetric import rsa

    header_b64, payload_b64, signature_b64 = token.split(".")
    signing_input = f"{header_b64}.{payload_b64}".encode()
    header = _json.loads(_b64url_decode(header_b64))
    alg = str(header.get("alg", ""))
    key = serialization.load_der_public_key(public_key_der)
    signature = _b64url_decode(signature_b64)

    if alg.startswith("ES"):
        bits = {"ES256": 256, "ES384": 384, "ES512": 521}[alg]
        sha = {"ES256": hashes.SHA256, "ES384": hashes.SHA384, "ES512": hashes.SHA512}[alg]
        der_signature = _p1363_to_der(signature)
        key.verify(der_signature, signing_input, ec.ECDSA(sha()))
        return True, alg
    if alg.startswith("RS"):
        sha = {"RS256": hashes.SHA256, "RS384": hashes.SHA384, "RS512": hashes.SHA512}[alg]
        key.verify(signature, signing_input, padding.PKCS1v15(), sha())
        return True, alg
    if alg.startswith("PS"):
        sha = {"PS256": hashes.SHA256, "PS384": hashes.SHA384, "PS512": hashes.SHA512}[alg]
        key.verify(
            signature,
            signing_input,
            padding.PSS(mgf=padding.MGF1(sha()), salt_length=padding.AutoAutoSaltLength if hasattr(padding, "AutoAutoSaltLength") else padding.MAX_LENGTH),
            sha(),
        )
        return True, alg
    return False, alg


def _int32_be(value: int) -> bytes:
    return value.to_bytes(4, "big", signed=True)


def _int64_be(value: int) -> bytes:
    return value.to_bytes(8, "big", signed=True)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _der_to_p1363(der_signature: bytes, key_size_bits: int) -> tuple[bytes, bytes]:
    """Split a DER ECDSA signature into raw r||s."""
    size = (key_size_bits + 7) // 8
    # DER: 0x30 len 0x02 rlen r 0x02 slen s
    index = 2
    r_len = der_signature[index + 1]
    r = der_signature[index + 2 : index + 2 + r_len]
    index = index + 2 + r_len
    s_len = der_signature[index + 1]
    s = der_signature[index + 2 : index + 2 + s_len]
    r_padded = r.lstrip(b"\x00").rjust(size, b"\x00")
    s_padded = s.lstrip(b"\x00").rjust(size, b"\x00")
    return r_padded, s_padded


def _p1363_to_der(raw_signature: bytes) -> bytes:
    half = len(raw_signature) // 2
    r = int.from_bytes(raw_signature[:half], "big")
    s = int.from_bytes(raw_signature[half:], "big")

    def _int_to_der_bytes(value: int) -> bytes:
        if value == 0:
            return b"\x00"
        out = value.to_bytes((value.bit_length() + 7) // 8, "big")
        if out[0] & 0x80:  # DER: positive integers need a leading zero byte
            out = b"\x00" + out
        return out

    r_bytes = _int_to_der_bytes(r)
    s_bytes = _int_to_der_bytes(s)
    content = (
        b"\x02"
        + bytes([len(r_bytes)])
        + r_bytes
        + b"\x02"
        + bytes([len(s_bytes)])
        + s_bytes
    )
    return b"\x30" + bytes([len(content)]) + content
