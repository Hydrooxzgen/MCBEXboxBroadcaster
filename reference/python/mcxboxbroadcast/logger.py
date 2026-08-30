"""Logging helper, mirroring the Java ``Logger`` class."""

from __future__ import annotations

import logging
import traceback
from typing import Optional


class Logger:
    """A small logger wrapper that supports prefixed child loggers."""

    def __init__(self, name: str = "MCXboxBroadcast", debug: bool = False) -> None:
        self._logger = logging.getLogger(name)
        # Don't propagate to the root logger if it has no handlers configured
        if not self._logger.handlers and not self._logger.propagate:
            pass
        self._debug = debug

    # -- configuration ------------------------------------------------------
    def set_debug(self, debug: bool) -> None:
        self._debug = debug

    def prefixed(self, prefix: str) -> "Logger":
        """Return a child logger with the given prefix."""
        if not prefix:
            return self
        return Logger(f"{self._logger.name}.{prefix}", self._debug)

    # -- helpers -----------------------------------------------------------
    def get_stack_trace(self, exc: BaseException) -> str:
        return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

    # -- logging methods ---------------------------------------------------
    def _log(self, level: int, msg: str, *args, exc_info: Optional[BaseException] = None) -> None:
        if level == logging.DEBUG and not self._debug:
            return
        self._logger.log(level, msg, *args, exc_info=exc_info)

    def info(self, msg: str, *args) -> None:
        self._log(logging.INFO, msg, *args)

    def warn(self, msg: str, *args) -> None:
        self._log(logging.WARNING, msg, *args)

    def error(self, msg: str, *args, exc_info: Optional[BaseException] = None) -> None:
        self._log(logging.ERROR, msg, *args, exc_info=exc_info)

    def debug(self, msg: str, *args) -> None:
        self._log(logging.DEBUG, msg, *args)

    def trace(self, msg: str, *args) -> None:
        self._log(logging.DEBUG, msg, *args)


def setup_logging(debug: bool = False) -> Logger:
    """Configure root logging and return the main application logger."""
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "[%(asctime)s] [%(name)s/%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    logger = Logger("MCXboxBroadcast", debug)
    return logger
