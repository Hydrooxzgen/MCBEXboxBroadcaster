"""Exceptions, ported from the Java core.exceptions package."""

from __future__ import annotations


class AgeVerificationException(RuntimeError):
    pass


class SessionCreationException(Exception):
    pass


class SessionUpdateException(Exception):
    pass


class XboxFriendsException(Exception):
    pass
