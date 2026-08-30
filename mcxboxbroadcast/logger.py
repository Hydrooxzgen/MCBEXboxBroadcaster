"""Logger interface, ported from Java Logger.java.

A minimal logging.Logger subclass carrying an optional prefix, with a debug
toggle that can be flipped at runtime (mirrors StandaloneLoggerImpl).
"""

from __future__ import annotations

import logging
import traceback


class Logger:
    def __init__(self, prefix: str = "", debug: bool = False) -> None:
        self._prefix = prefix
        self._debug_enabled = debug

    # -- configuration -------------------------------------------------
    def set_debug(self, debug: bool) -> None:
        self._debug_enabled = debug

    def prefixed(self, prefix: str) -> "Logger":
        return Logger(self._prefix + prefix, self._debug_enabled)

    # -- output --------------------------------------------------------
    def _fmt(self, message: str) -> str:
        return f"[{self._prefix}] {message}" if self._prefix else message

    def info(self, message: str) -> None:
        logging.info(self._fmt(message))

    def warn(self, message: str) -> None:
        logging.warning(self._fmt(message))

    def error(self, message: str, ex: BaseException | None = None) -> None:
        if ex is not None:
            logging.error(self._fmt(message) + "\n" + self.get_stack_trace(ex))
        else:
            logging.error(self._fmt(message))

    def debug(self, message: str) -> None:
        if self._debug_enabled:
            logging.debug(self._fmt(message))

    @staticmethod
    def get_stack_trace(ex: BaseException) -> str:
        return "".join(traceback.format_exception(type(ex), ex, ex.__traceback__))


def setup_console_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
