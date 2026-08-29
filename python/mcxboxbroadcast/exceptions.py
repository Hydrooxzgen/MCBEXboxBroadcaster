"""Exceptions mirroring the Java ``core/exceptions`` package."""

from __future__ import annotations


class SessionCreationException(Exception):
    pass


class SessionUpdateException(Exception):
    pass


class XboxFriendsException(Exception):
    pass


class AgeVerificationException(Exception):
    pass


__all__ = [
    "SessionCreationException",
    "SessionUpdateException",
    "XboxFriendsException",
    "AgeVerificationException",
]
