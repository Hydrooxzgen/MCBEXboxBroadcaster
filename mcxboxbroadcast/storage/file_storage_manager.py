"""File based storage manager with a sqlite player history, ported from
Java FileStorageManager.java."""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

from ..logger import Logger
from .storage_manager import PlayerHistoryStorage, StorageManager

logger = Logger("Storage")


class SqlitePlayerHistoryStorage(PlayerHistoryStorage):
    def __init__(self, db_file: str) -> None:
        self._first_run = not os.path.exists(db_file)
        # Accessed from both the main thread and the asyncio network thread
        self._connection = sqlite3.connect(db_file, check_same_thread=False)
        self._connection_lock = threading.Lock()
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS players ("
            "xuid VARCHAR(32), lastSeen INTEGER, PRIMARY KEY(xuid));"
        )
        self._connection.commit()

    def is_first_run(self) -> bool:
        return self._first_run

    def last_seen(self, xuid: str, last_seen: Optional[datetime] = None) -> Optional[datetime]:
        try:
            with self._connection_lock:
                if last_seen is not None:
                    with self._connection as conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO players (xuid, lastSeen) VALUES (?, ?);",
                            (xuid, int(last_seen.timestamp())),
                        )
                    return None
                cursor = self._connection.execute(
                    "SELECT lastSeen FROM players WHERE xuid = ?;", (xuid,)
                )
                row = cursor.fetchone()
            if row is None:
                return None
            return datetime.fromtimestamp(row[0], tz=timezone.utc)
        except sqlite3.Error as ex:
            raise IOError(f"Failed to access player history for xuid: {xuid}") from ex

    def clear(self, xuid: str) -> None:
        try:
            with self._connection_lock, self._connection as conn:
                conn.execute("DELETE FROM players WHERE xuid = ?;", (xuid,))
        except sqlite3.Error as ex:
            raise IOError(f"Failed to remove player history for xuid: {xuid}") from ex

    def all(self) -> dict[str, datetime]:
        try:
            with self._connection_lock:
                cursor = self._connection.execute("SELECT xuid, lastSeen FROM players;")
                rows = cursor.fetchall()
            return {
                xuid: datetime.fromtimestamp(last_seen, tz=timezone.utc)
                for xuid, last_seen in rows
            }
        except sqlite3.Error as ex:
            raise IOError("Failed to retrieve all player history") from ex


class FileStorageManager(StorageManager):
    def __init__(self, cache_folder: str, screenshot_path: str) -> None:
        self._cache_folder = cache_folder
        self._screenshot_path = screenshot_path
        os.makedirs(cache_folder, exist_ok=True)
        self._player_history = SqlitePlayerHistoryStorage(
            os.path.join(cache_folder, "player_history.db")
        )

    def _read(self, file: str) -> str:
        cache_file = os.path.join(self._cache_folder, file)
        if not os.path.exists(cache_file):
            return ""
        with open(cache_file, "r", encoding="utf-8") as fh:
            return fh.read()

    def _write(self, file: str, data: Optional[str]) -> None:
        file_path = os.path.join(self._cache_folder, file)
        if data is None or not data.strip():
            if os.path.exists(file_path):
                os.remove(file_path)
            return
        with open(file_path, "w", encoding="utf-8") as fh:
            fh.write(data)

    def cache(self, data: Optional[str] = None) -> str:
        if data is not None:
            self._write("cache.json", data)
        return self._read("cache.json")

    def sub_sessions(self, data: Optional[str] = None) -> str:
        if data is not None:
            self._write("sub_sessions.json", data)
        return self._read("sub_sessions.json")

    def last_session_response(self, data: Optional[str] = None) -> str:
        if data is not None:
            self._write("lastSessionResponse.json", data)
        return self._read("lastSessionResponse.json")

    def current_session_response(self, data: Optional[str] = None) -> str:
        if data is not None:
            self._write("currentSessionResponse.json", data)
        return self._read("currentSessionResponse.json")

    def sub_session(self, id: str) -> "FileStorageManager":
        return FileStorageManager(os.path.join(self._cache_folder, id), self._screenshot_path)

    def screenshot(self) -> Optional[bytes]:
        if not os.path.exists(self._screenshot_path):
            return None
        with open(self._screenshot_path, "rb") as fh:
            return fh.read()

    def screenshot_last_modified(self) -> float:
        try:
            return os.path.getmtime(self._screenshot_path)
        except OSError:
            return 0.0

    def cleanup(self) -> None:
        shutil.rmtree(self._cache_folder, ignore_errors=True)

    def player_history(self) -> PlayerHistoryStorage:
        return self._player_history
