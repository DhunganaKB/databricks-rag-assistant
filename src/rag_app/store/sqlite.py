"""SQLite conversation store for local development and tests (single process)."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from rag_app.store.base import uuid7

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id     TEXT PRIMARY KEY,
  user_id        TEXT NOT NULL,
  title          TEXT,
  status         TEXT NOT NULL DEFAULT 'active',
  turn_count     INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  last_active_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id, last_active_at);
CREATE TABLE IF NOT EXISTS messages (
  message_id        TEXT PRIMARY KEY,
  session_id        TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
  turn_index        INTEGER NOT NULL,
  role              TEXT NOT NULL,
  content           TEXT NOT NULL,
  client_message_id TEXT,
  standalone_query  TEXT,
  citations         TEXT,
  request_id        TEXT,
  latency_ms        INTEGER,
  created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (session_id, turn_index, role)
);
CREATE UNIQUE INDEX IF NOT EXISTS messages_client_id ON messages(session_id, client_message_id)
  WHERE client_message_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS feedback (
  feedback_id TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL,
  request_id  TEXT NOT NULL,
  rating      INTEGER NOT NULL,
  comment     TEXT,
  created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


def _message(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["citations"] = json.loads(d["citations"]) if d.get("citations") else []
    return d


class SQLiteStore:
    def __init__(self, path: str = ".data/rag_app.db"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()

    @contextmanager
    def _tx(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def _q(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def ensure_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)

    # sessions ---------------------------------------------------------------
    def create_session(self, user_id: str) -> dict:
        sid = uuid7()
        with self._tx() as c:
            c.execute("INSERT INTO sessions (session_id, user_id) VALUES (?, ?)", (sid, user_id))
        return self.get_session(sid, user_id)

    def get_session(self, session_id: str, user_id: str) -> dict | None:
        rows = self._q(
            "SELECT * FROM sessions WHERE session_id = ? AND user_id = ? AND status = 'active'", (session_id, user_id)
        )
        return rows[0] if rows else None

    def list_sessions(self, user_id: str, limit: int = 50) -> list[dict]:
        return self._q(
            "SELECT session_id, title, turn_count, created_at, last_active_at FROM sessions "
            "WHERE user_id = ? AND status = 'active' ORDER BY last_active_at DESC LIMIT ?",
            (user_id, limit),
        )

    def delete_session(self, session_id: str, user_id: str) -> bool:
        with self._tx() as c:
            return (
                c.execute("DELETE FROM sessions WHERE session_id = ? AND user_id = ?", (session_id, user_id)).rowcount
                > 0
            )

    def set_title(self, session_id: str, user_id: str, title: str) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE sessions SET title = ? WHERE session_id = ? AND user_id = ?", (title, session_id, user_id)
            )

    # messages ---------------------------------------------------------------
    def recent_turns(self, session_id: str, user_id: str, turns: int) -> list[dict]:
        if turns <= 0:
            return []
        rows = self._q(
            "SELECT m.turn_index, m.role, m.content FROM messages m JOIN sessions s USING (session_id) "
            "WHERE m.session_id = ? AND s.user_id = ? AND m.turn_index > s.turn_count - ? "
            "ORDER BY m.turn_index, CASE m.role WHEN 'user' THEN 0 ELSE 1 END",
            (session_id, user_id, turns),
        )
        return rows

    def messages(self, session_id: str, user_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.* FROM messages m JOIN sessions s USING (session_id) WHERE m.session_id = ? AND s.user_id = ? "
                "ORDER BY m.turn_index, CASE m.role WHEN 'user' THEN 0 ELSE 1 END",
                (session_id, user_id),
            ).fetchall()
        return [_message(r) for r in rows]

    def find_turn_by_client_id(self, session_id: str, user_id: str, client_message_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT a.* FROM messages u JOIN messages a ON a.session_id = u.session_id "
                "AND a.turn_index = u.turn_index AND a.role = 'assistant' JOIN sessions s ON s.session_id = u.session_id "
                "WHERE u.session_id = ? AND s.user_id = ? AND u.client_message_id = ?",
                (session_id, user_id, client_message_id),
            ).fetchone()
        return _message(row) if row else None

    def append_turn(
        self,
        session_id: str,
        user_id: str,
        question: str,
        answer: str,
        *,
        client_message_id: str | None,
        standalone_query: str,
        citations: list[dict],
        request_id: str,
        latency_ms: int,
    ) -> int:
        with self._tx() as c:
            row = c.execute(
                "SELECT turn_count FROM sessions WHERE session_id = ? AND user_id = ?", (session_id, user_id)
            ).fetchone()
            if row is None:
                raise PermissionError("session not found for this user")
            turn = row[0] + 1
            c.execute(
                "INSERT INTO messages (message_id, session_id, turn_index, role, content, client_message_id) "
                "VALUES (?, ?, ?, 'user', ?, ?)",
                (uuid7(), session_id, turn, question, client_message_id),
            )
            c.execute(
                "INSERT INTO messages (message_id, session_id, turn_index, role, content, standalone_query, "
                "citations, request_id, latency_ms) VALUES (?, ?, ?, 'assistant', ?, ?, ?, ?, ?)",
                (uuid7(), session_id, turn, answer, standalone_query, json.dumps(citations), request_id, latency_ms),
            )
            c.execute(
                "UPDATE sessions SET turn_count = ?, last_active_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE session_id = ?",
                (turn, session_id),
            )
            return turn

    def add_feedback(self, user_id: str, request_id: str, rating: int, comment: str | None) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO feedback (feedback_id, user_id, request_id, rating, comment) VALUES (?, ?, ?, ?, ?)",
                (uuid7(), user_id, request_id, rating, comment),
            )
