"""SQLite persistence for chat sessions.

Uses the standard library only. Calls are synchronous and short; the API layer
runs them in a worker thread so the event loop is never blocked.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,          -- 'user' or 'assistant'
    content    TEXT NOT NULL,
    metadata   TEXT,                   -- JSON string, assistant messages only
    created_at TEXT NOT NULL           -- server time, ISO-8601
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id, id);

CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    system_prompt TEXT,                -- per-session override, NULL = use default
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            directory = os.path.dirname(os.path.abspath(path))
            os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)  # schema is created automatically on startup

    # -- health -----------------------------------------------------------
    def ping(self) -> bool:
        try:
            with self._lock:
                self._conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # -- reads ------------------------------------------------------------
    def load_context(self, session_id: str, limit: int) -> tuple[str | None, list[dict[str, str]]]:
        """Stored system prompt and the last `limit` messages (oldest first)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT system_prompt FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            rows = self._conn.execute(
                "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
        while history and history[0]["role"] != "user":  # never start the window mid-exchange
            history.pop(0)
        return (row["system_prompt"] if row else None), history

    def get_messages(self, session_id: str) -> list[dict[str, Any]] | None:
        """Full history, oldest first. None if the session does not exist."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content, metadata FROM messages WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
        if not rows:
            return None
        out: list[dict[str, Any]] = []
        for r in rows:
            item: dict[str, Any] = {"role": r["role"], "content": r["content"]}
            if r["role"] == "assistant" and r["metadata"]:
                item["metadata"] = json.loads(r["metadata"])
            out.append(item)
        return out

    # -- writes -----------------------------------------------------------
    def save_exchange(
        self,
        session_id: str,
        user_message: str,
        reply: str,
        metadata: dict[str, Any],
        system_prompt: str | None = None,
    ) -> None:
        """Persist one successful exchange atomically (both messages or neither)."""
        now = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO sessions (session_id, system_prompt, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(session_id) DO UPDATE SET "
                    "  system_prompt = COALESCE(excluded.system_prompt, sessions.system_prompt), "
                    "  updated_at = excluded.updated_at",
                    (session_id, system_prompt, now, now),
                )
                self._conn.execute(
                    "INSERT INTO messages (session_id, role, content, metadata, created_at) "
                    "VALUES (?, 'user', ?, NULL, ?)",
                    (session_id, user_message, now),
                )
                self._conn.execute(
                    "INSERT INTO messages (session_id, role, content, metadata, created_at) "
                    "VALUES (?, 'assistant', ?, ?, ?)",
                    (session_id, reply, json.dumps(metadata), _now()),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()
