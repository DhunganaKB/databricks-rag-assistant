"""In-memory SQLite store: a test double with the same behavior as PostgresStore.

It never touches the file system (the database lives only in process memory), so unit
tests and offline tools run without Lakebase while the app itself stores nothing locally.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager

from rag_app.store.base import normalize_fact, uuid7

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT, status TEXT NOT NULL DEFAULT 'active',
  turn_count INTEGER NOT NULL DEFAULT 0, running_summary TEXT NOT NULL DEFAULT '',
  summary_upto_turn INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT ({_NOW}), last_active_at TEXT NOT NULL DEFAULT ({_NOW}));
CREATE TABLE IF NOT EXISTS messages (
  message_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
  turn_index INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, client_message_id TEXT,
  standalone_query TEXT, citations TEXT, request_id TEXT, latency_ms INTEGER,
  created_at TEXT NOT NULL DEFAULT ({_NOW}), UNIQUE (session_id, turn_index, role));
CREATE UNIQUE INDEX IF NOT EXISTS messages_client_id ON messages(session_id, client_message_id)
  WHERE client_message_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS user_memories (
  memory_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, memory_type TEXT NOT NULL, content TEXT NOT NULL,
  normalized TEXT NOT NULL, source_session_id TEXT, status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL DEFAULT ({_NOW}), updated_at TEXT NOT NULL DEFAULT ({_NOW}), last_used_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS user_memories_dedup ON user_memories(user_id, normalized) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS feedback (
  feedback_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, request_id TEXT NOT NULL, rating INTEGER NOT NULL,
  comment TEXT, created_at TEXT NOT NULL DEFAULT ({_NOW}));
"""


def _message(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["citations"] = json.loads(d["citations"]) if d.get("citations") else []
    return d


class SQLiteStore:
    def __init__(self):
        self._conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
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

    def ensure_schema(self) -> list[str]:
        with self._lock:
            self._conn.executescript(SCHEMA)
        return ["001_initial"]

    # sessions ---------------------------------------------------------------------
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

    def update_summary(self, session_id: str, user_id: str, summary: str, upto_turn: int) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE sessions SET running_summary = ?, summary_upto_turn = ? "
                "WHERE session_id = ? AND user_id = ? AND summary_upto_turn < ?",
                (summary, upto_turn, session_id, user_id, upto_turn),
            )

    # messages ---------------------------------------------------------------------
    def turns_between(self, session_id: str, user_id: str, after_turn: int, upto_turn: int | None = None) -> list[dict]:
        return self._q(
            "SELECT m.turn_index, m.role, m.content FROM messages m JOIN sessions s USING (session_id) "
            "WHERE m.session_id = ? AND s.user_id = ? AND m.turn_index > ? AND (? IS NULL OR m.turn_index <= ?) "
            "ORDER BY m.turn_index, CASE m.role WHEN 'user' THEN 0 ELSE 1 END",
            (session_id, user_id, after_turn, upto_turn, upto_turn),
        )

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
                "INSERT INTO messages (message_id, session_id, turn_index, role, content, standalone_query, citations, "
                "request_id, latency_ms) VALUES (?, ?, ?, 'assistant', ?, ?, ?, ?, ?)",
                (uuid7(), session_id, turn, answer, standalone_query, json.dumps(citations), request_id, latency_ms),
            )
            c.execute(
                f"UPDATE sessions SET turn_count = ?, last_active_at = {_NOW} WHERE session_id = ?", (turn, session_id)
            )
            return turn

    # long-term memory ------------------------------------------------------------------
    def list_memories(self, user_id: str, limit: int = 50) -> list[dict]:
        return self._q(
            "SELECT memory_id, memory_type, content, created_at, updated_at, last_used_at FROM user_memories "
            "WHERE user_id = ? AND status = 'active' ORDER BY updated_at DESC, rowid DESC LIMIT ?",
            (user_id, limit),
        )

    def add_memories(self, user_id: str, items: list[dict], session_id: str | None) -> int:
        added = 0
        with self._tx() as c:
            for item in items:
                added += c.execute(
                    "INSERT OR IGNORE INTO user_memories (memory_id, user_id, memory_type, content, normalized, "
                    "source_session_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (uuid7(), user_id, item["type"], item["content"], normalize_fact(item["content"]), session_id),
                ).rowcount
        return added

    def delete_memory(self, user_id: str, memory_id: str | None = None) -> int:
        with self._tx() as c:  # hard delete, like PostgresStore
            return c.execute(
                "DELETE FROM user_memories WHERE user_id = ? AND (? IS NULL OR memory_id = ?)",
                (user_id, memory_id, memory_id),
            ).rowcount

    def touch_memories(self, user_id: str, memory_ids: list[str]) -> None:
        with self._tx() as c:
            for mid in memory_ids:
                c.execute(
                    f"UPDATE user_memories SET last_used_at = {_NOW} WHERE user_id = ? AND memory_id = ?",
                    (user_id, mid),
                )

    # feedback ---------------------------------------------------------------------------
    def add_feedback(self, user_id: str, request_id: str, rating: int, comment: str | None) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO feedback (feedback_id, user_id, request_id, rating, comment) VALUES (?, ?, ?, ?, ?)",
                (uuid7(), user_id, request_id, rating, comment),
            )
