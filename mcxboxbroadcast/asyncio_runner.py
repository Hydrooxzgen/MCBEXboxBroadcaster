"""A shared asyncio event loop running on a background thread.

The Java version uses blocking websockets (java-websocket) and Netty; the
Python port centralizes all async IO (RTA websocket, NetherNet signaling,
aiortc peer connections) on one loop and exposes thread-safe bridges for the
synchronous session manager code.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Any, Coroutine, Optional


class AsyncioRunner:
    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="MCXboxBroadcast-Async", daemon=True
        )
        self._thread.start()
        self._started.wait()

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._started.set()
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        self.start()
        return self._loop

    def submit(self, coro: Coroutine) -> concurrent.futures.Future:
        """Schedule a coroutine on the loop, returning a concurrent Future."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def run(self, coro: Coroutine, timeout: Optional[float] = None) -> Any:
        """Schedule a coroutine and block the calling thread for its result."""
        future = self.submit(coro)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise

    def call_soon_threadsafe(self, callback, *args) -> None:
        self.loop.call_soon_threadsafe(callback, *args)

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)


_runner: Optional[AsyncioRunner] = None
_runner_lock = threading.Lock()


def shared_runner() -> AsyncioRunner:
    global _runner
    with _runner_lock:
        if _runner is None:
            _runner = AsyncioRunner()
        return _runner
