"""NetherNet server identity (a=identity SDP augmentation).

Ported from kastle's ServerIdentity/Identity classes. The answer SDP must
carry an a=identity attribute containing a base64 JSON blob with an ES384
JWS token (cpk claim = the P-384 public key) and a fingerprint assertion.
"""

from __future__ import annotations

import base64
import json
import time
from typing import List, Tuple

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from ..auth.crypto import encode_public_key_spki_b64, _der_to_p1363, _b64url, _b64url_decode


class ServerIdentity:
    def __init__(self, domain: str = "self") -> None:
        self.domain = domain
        from cryptography.hazmat.primitives.asymmetric import ec as _ec

        self._private_key = _ec.generate_private_key(_ec.SECP384R1())
        self._public_key = self._private_key.public_key()
        self._token = self._build_token()

    def _build_token(self) -> str:
        header = {"alg": "ES384"}
        payload = {
            "cpk": encode_public_key_spki_b64(self._public_key),
            "iat": int(time.time()),
        }
        if self.domain:
            payload["iss"] = self.domain
        signing_input = (
            _b64url(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + _b64url(json.dumps(payload, separators=(",", ":")).encode())
        )
        return signing_input + "." + self._sign(signing_input.encode())

    def _sign(self, data: bytes) -> str:
        der = self._private_key.sign(data, ec.ECDSA(hashes.SHA384()))
        r, s = _der_to_p1363(der, 384)
        return _b64url(r + s)

    @staticmethod
    def _canonical_fingerprint_json(answer_sdp: str) -> str:
        prefix = "a=fingerprint:"
        entries = []
        for line in answer_sdp.splitlines():
            line = line.strip()
            if line.startswith(prefix):
                value = line[len(prefix) :].strip()
                parts = value.split(" ")
                if len(parts) != 2:
                    raise ValueError(f"Invalid fingerprint line: {line}")
                entries.append(
                    '{"algorithm":"' + parts[0] + '","digest":"' + parts[1] + '"}'
                )
        if not entries:
            raise ValueError("Answer SDP contains no fingerprint line")
        return '{"fingerprint":[' + ",".join(entries) + "]}"

    def _fingerprint_assertion(self, answer_sdp: str) -> str:
        canonical = self._canonical_fingerprint_json(answer_sdp)
        jws = self._sign_jws(canonical.encode())
        parts = jws.split(".")
        return parts[0] + ".." + parts[2]

    def _sign_jws(self, payload: bytes) -> str:
        header = {"alg": "ES384"}
        signing_input = (
            _b64url(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + _b64url(payload)
        )
        return signing_input + "." + self._sign(signing_input.encode())

    def identity_value(self, answer_sdp: str) -> str:
        # Mirrors kastle's Identity.toJson(): the assertion is double-serialized
        # (an object rendered to a string inside the outer JSON)
        assertion = json.dumps(
            {
                "token": self._token,
                "fingerprints": self._fingerprint_assertion(answer_sdp),
            },
            separators=(",", ":"),
        )
        identity = {
            "idp": {"domain": self.domain, "protocol": "default"},
            "assertion": assertion,
        }
        return base64.b64encode(
            json.dumps(identity, separators=(",", ":")).encode()
        ).decode()

    def augment_answer(self, answer_sdp: str) -> str:
        line = "a=identity:" + self.identity_value(answer_sdp)
        eol = "\r\n" if "\r\n" in answer_sdp else "\n"
        lines = answer_sdp.replace("\r\n", "\n").split("\n")
        out: List[str] = []
        inserted = False
        for current in lines:
            if not inserted and current.startswith("m="):
                out.append(line)
                inserted = True
            out.append(current)
        if not inserted:
            out.append(line)
        return eol.join(out)
