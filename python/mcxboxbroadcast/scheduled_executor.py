"""A small scheduled-executor shim mirroring the Java ``ScheduledExecutorService``.

Provides ``submit``, ``schedule`` and ``schedule_with_fixed_delay`` backed by a
``ThreadPoolExecutor`` plus chained ``threading.Timer`` instances.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Optional


class ScheduledExecutor:
    def __init__(self, name: str = "MCXboxBroadcast", max_workers: int = 5) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=name
        )
        self._timers: set = set()
        self._lock = threading.Lock()
        self._shutdown = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def submit(self, fn: Callable[[], None], *args, **kwargs) -> Future:
        return self._executor.submit(fn, *args, **kwargs)

    def schedule(self, fn: Callable[[], None], delay: float) -> "threading.Timer":
        """Run ``fn`` once after ``delay`` seconds."""

        def run(timer: "threading.Timer") -> None:
            try:
                fn()
            except Exception:
                pass
            finally:
                with self._lock:
                    self._timers.discard(timer)

        timer = threading.Timer(delay, lambda: run(timer))
        with self._lock:
            self._timers.add(timer)
        timer.start()
        return timer

    def schedule_with_fixed_delay(
        self,
        fn: Callable[[], None],
        initial_delay: float,
        delay: float,
    ) -> None:
        """Run ``fn`` repeatedly; the next run starts ``delay`` after the previous ends."""

        def loop() -> None:
            if self._shutdown:
                return
            try:
                fn()
            except Exception:
                pass
            finally:
                self.schedule(loop, delay)

        self.schedule(loop, initial_delay)

    def shutdown(self) -> None:
        self._shutdown = True
        with self._lock:
            for timer in list(self._timers):
                timer.cancel()
            self._timers.clear()
        self._executor.shutdown(wait=False)
