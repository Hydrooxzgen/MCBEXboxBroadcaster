"""A small thread-pool based scheduler mirroring Java's
ScheduledExecutorService (the subset MCXboxBroadcast uses)."""

from __future__ import annotations

import concurrent.futures
import threading
from typing import Callable, Optional


class ScheduledFuture:
    def __init__(self) -> None:
        self._done = threading.Event()
        self.cancelled = False

    def is_done(self) -> bool:
        return self._done.is_set() or self.cancelled

    def _mark_done(self) -> None:
        self._done.set()

    def cancel(self) -> None:
        self.cancelled = True
        self._done.set()


class ScheduledExecutorService:
    def __init__(self, workers: int = 5) -> None:
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="MCXboxBroadcast"
        )
        self._timers: list[threading.Timer] = []
        self._lock = threading.Lock()
        self._shutdown = False

    def execute(self, fn: Callable[[], None]) -> None:
        self._pool.submit(self._safe, fn)

    def submit(self, fn: Callable[[], None]) -> ScheduledFuture:
        future = ScheduledFuture()

        def run() -> None:
            if not future.cancelled:
                self._safe(fn)
                future._mark_done()

        self._pool.submit(run)
        return future

    def schedule(self, fn: Callable[[], None], delay: float, time_unit_seconds: float = 1.0) -> ScheduledFuture:
        """Schedule a one-shot run after `delay` seconds (Java: schedule)."""
        return self._schedule_internal(fn, delay)

    def schedule_with_fixed_delay(
        self, fn: Callable[[], None], initial_delay: float, delay: float, time_unit_seconds: float = 1.0
    ) -> ScheduledFuture:
        """Java: scheduleWithFixedDelay - the delay is between the END of one
        run and the start of the next."""
        future = ScheduledFuture()

        def run() -> None:
            if future.cancelled or self._shutdown:
                future._mark_done()
                return
            try:
                self._safe(fn)
            finally:
                if not future.cancelled and not self._shutdown:
                    timer = threading.Timer(delay, run)
                    timer.daemon = True
                    with self._lock:
                        self._timers.append(timer)
                    timer.start()
                else:
                    future._mark_done()

        timer = threading.Timer(initial_delay, run)
        timer.daemon = True
        with self._lock:
            self._timers.append(timer)
        timer.start()
        return future

    def _schedule_internal(self, fn: Callable[[], None], delay: float) -> ScheduledFuture:
        future = ScheduledFuture()

        def run() -> None:
            if not future.cancelled:
                self._safe(fn)
            future._mark_done()

        timer = threading.Timer(delay, run)
        timer.daemon = True
        with self._lock:
            self._timers.append(timer)
        timer.start()
        return future

    @staticmethod
    def _safe(fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            import logging

            logging.exception("Unhandled exception in scheduled task")

    def shutdown_now(self) -> None:
        self._shutdown = True
        with self._lock:
            for timer in self._timers:
                timer.cancel()
            self._timers.clear()
        self._pool.shutdown(wait=False, cancel_futures=True)
