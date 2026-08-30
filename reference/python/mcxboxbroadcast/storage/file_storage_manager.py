"""File-backed storage manager, mirroring the Java ``FileStorageManager``."""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime
from typing import Mapping, Optional

from .storage_manager import PlayerHistoryStorage, StorageManager


class _SqlitePlayerHistoryStorage(PlayerHistoryStorage):
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._first_run = not os.path.exists(db_path)
        # check_same_thread=False because friend syncing runs on a background
        # thread while the session manager uses the main thread. A lock keeps
        # the single connection safe across those threads.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS players (xuid VARCHAR(32) PRIMARY KEY, lastSeen INTEGER);"
        )
        self._conn.commit()

    def is_first_run(self) -> bool:
        return self._first_run

    def last_seen(self, xuid: str, last_seen: Optional[datetime] = None) -> Optional[datetime]:
        with self._lock:
            if last_seen is None:
                cur = self._conn.execute("SELECT lastSeen FROM players WHERE xuid = ?;", (xuid,))
                row = cur.fetchone()
                if row is None:
                    return None
                return datetime.fromtimestamp(row[0])
            self._conn.execute(
                "INSERT OR REPLACE INTO players (xuid, lastSeen) VALUES (?, ?);",
                (xuid, int(last_seen.timestamp())),
            )
            self._conn.commit()
            return None

    def clear(self, xuid: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM players WHERE xuid = ?;", (xuid,))
            self._conn.commit()

    def all(self) -> Mapping[str, datetime]:
        with self._lock:
            cur = self._conn.execute("SELECT xuid, lastSeen FROM players;")
            return {row[0]: datetime.fromtimestamp(row[1]) for row in cur.fetchall()}


class FileStorageManager(StorageManager):
    def __init__(self, cache_folder: str, screenshot_path: str) -> None:
        self._cache_folder = cache_folder
        self._screenshot_path = screenshot_path
        os.makedirs(cache_folder, exist_ok=True)
        self._player_history = _SqlitePlayerHistoryStorage(
            os.path.join(cache_folder, "player_history.db")
        )

    # -- low level helpers -------------------------------------------------
    def _read(self, filename: str) -> str:
        path = os.path.join(self._cache_folder, filename)
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def _write(self, filename: str, data: str) -> None:
        path = os.path.join(self._cache_folder, filename)
        if data is None or data.strip() == "":
            if os.path.exists(path):
                os.remove(path)
            return
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(data)

    # -- cache -------------------------------------------------------------
    def cache(self, data: Optional[str] = None) -> Optional[str]:
        if data is None:
            return self._read("cache.json")
        self._write("cache.json", data)
        return None

    # -- sub sessions ------------------------------------------------------
    def sub_sessions(self, data: Optional[str] = None) -> Optional[str]:
        if data is None:
            return self._read("sub_sessions.json")
        self._write("sub_sessions.json", data)
        return None

    # -- session responses -------------------------------------------------
    def last_session_response(self, data: Optional[str] = None) -> Optional[str]:
        if data is None:
            return self._read("lastSessionResponse.json")
        self._write("lastSessionResponse.json", data)
        return None

    def current_session_response(self, data: Optional[str] = None) -> Optional[str]:
        if data is None:
            return self._read("currentSessionResponse.json")
        self._write("currentSessionResponse.json", data)
        return None

    # -- misc --------------------------------------------------------------
    def sub_session(self, session_id: str) -> "FileStorageManager":
        return FileStorageManager(
            os.path.join(self._cache_folder, session_id), self._screenshot_path
        )

    def screenshot(self) -> str:
        return self._screenshot_path

    def cleanup(self) -> None:
        for root, _dirs, files in os.walk(self._cache_folder, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            os.rmdir(root)

    def player_history(self) -> PlayerHistoryStorage:
        return self._player_history
