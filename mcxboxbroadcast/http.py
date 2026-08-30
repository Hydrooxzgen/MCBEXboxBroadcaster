"""Shared HTTP helpers (the Python counterpart of the Java Methanol client)."""

from __future__ import annotations

import requests

DEFAULT_TIMEOUT = 5.0  # matches the Java http.request.timeout default of 5000ms


def new_session() -> requests.Session:
    session = requests.Session()
    return session


def get(session: requests.Session, url: str, headers: dict, timeout: float = DEFAULT_TIMEOUT) -> requests.Response:
    return session.get(url, headers=headers, timeout=timeout)


def post_json(session: requests.Session, url: str, headers: dict, body, timeout: float = DEFAULT_TIMEOUT) -> requests.Response:
    headers = {**headers, "Content-Type": "application/json"}
    import json as _json

    return session.post(
        url, headers=headers, data=_json.dumps(body), timeout=timeout
    )


def put(session: requests.Session, url: str, headers: dict, body: bytes | None = None, timeout: float = DEFAULT_TIMEOUT) -> requests.Response:
    return session.put(url, headers=headers, data=body, timeout=timeout)


def delete(session: requests.Session, url: str, headers: dict, timeout: float = DEFAULT_TIMEOUT) -> requests.Response:
    return session.delete(url, headers=headers, timeout=timeout)


def post_raw(session: requests.Session, url: str, headers: dict, body: bytes, timeout: float = DEFAULT_TIMEOUT) -> requests.Response:
    return session.post(url, headers=headers, data=body, timeout=timeout)
