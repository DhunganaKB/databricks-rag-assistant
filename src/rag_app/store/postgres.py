"""Postgres (Lakebase) conversation store for the deployed app.

Lakebase accepts a short-lived Databricks OAuth token as the Postgres password, so each
new physical connection fetches a fresh credential and connections are recycled before
the token expires. In Databricks Apps the attached database resource sets PGHOST / PGUSER
/ PGDATABASE / PGPORT for the app's service principal.
"""

from __future__ import annotations

import json
import os
import re
import uuid

from rag_app.store.base import is_uuid, uuid7

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS {s};
CREATE TABLE IF NOT EXISTS {s}.sessions (
  session_id UUID PRIMARY KEY, user_id TEXT NOT NULL, title TEXT, status TEXT NOT NULL DEFAULT 'active',
  turn_count INT NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_active_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS sessions_user ON {s}.sessions (user_id, last_active_at DESC);
CREATE TABLE IF NOT EXISTS {s}.messages (
  message_id UUID PRIMARY KEY, session_id UUID NOT NULL REFERENCES {s}.sessions(session_id) ON DELETE CASCADE,
  turn_index INT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, client_message_id TEXT,
  standalone_query TEXT, citations JSONB, request_id TEXT, latency_ms INT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE (session_id, turn_index, role));
CREATE UNIQUE INDEX IF NOT EXISTS messages_client_id ON {s}.messages (session_id, client_message_id)
  WHERE client_message_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS {s}.feedback (
  feedback_id UUID PRIMARY KEY, user_id TEXT NOT NULL, request_id TEXT NOT NULL, rating SMALLINT NOT NULL,
  comment TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
"""


def make_pool(workspace_client, instance_name: str):
    import psycopg
    from psycopg_pool import ConnectionPool

    w = workspace_client
    host = os.getenv("PGHOST") or w.database.get_database_instance(instance_name).read_write_dns
    user = os.getenv("PGUSER") or w.current_user.me().user_name

    class TokenConnection(psycopg.Connection):
        @classmethod
        def connect(cls, conninfo: str = "", **kwargs):
            cred = w.database.generate_database_credential(request_id=str(uuid.uuid4()), instance_names=[instance_name])
            kwargs["password"] = cred.token
            return super().connect(conninfo, **kwargs)

    conninfo = (
        f"host={host} port={os.getenv('PGPORT', '5432')} dbname={os.getenv('PGDATABASE', 'databricks_postgres')} "
        f"user={user} sslmode=require"
    )
    return ConnectionPool(
        conninfo,
        connection_class=TokenConnection,
        min_size=1,
        max_size=8,
        max_lifetime=45 * 60,
        kwargs={"autocommit": True},
        open=True,
    )


class PostgresStore:
    def __init__(self, pool, schema: str = "rag_app"):
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", schema):
            raise ValueError("invalid schema name")
        self.pool, self.s = pool, schema

    def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        from psycopg.rows import dict_row

        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            rows = list(cur.fetchall()) if cur.description else []
        for r in rows:
            for k, v in list(r.items()):
                if isinstance(v, uuid.UUID):
                    r[k] = str(v)
        return rows

    def _exec(self, sql: str, params: tuple = ()) -> int:
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount

    def ensure_schema(self) -> None:
        with self.pool.connection() as conn:
            conn.execute(SCHEMA.format(s=self.s))

    def create_session(self, user_id: str) -> dict:
        return self._rows(
            f"INSERT INTO {self.s}.sessions (session_id, user_id) VALUES (%s, %s) RETURNING *", (uuid7(), user_id)
        )[0]

    def get_session(self, session_id: str, user_id: str) -> dict | None:
        if not is_uuid(session_id):
            return None
        rows = self._rows(
            f"SELECT * FROM {self.s}.sessions WHERE session_id = %s AND user_id = %s AND status = 'active'",
            (session_id, user_id),
        )
        return rows[0] if rows else None

    def list_sessions(self, user_id: str, limit: int = 50) -> list[dict]:
        return self._rows(
            f"SELECT session_id, title, turn_count, created_at, last_active_at FROM {self.s}.sessions "
            f"WHERE user_id = %s AND status = 'active' ORDER BY last_active_at DESC LIMIT %s",
            (user_id, limit),
        )

    def delete_session(self, session_id: str, user_id: str) -> bool:
        if not is_uuid(session_id):
            return False
        return (
            self._exec(f"DELETE FROM {self.s}.sessions WHERE session_id = %s AND user_id = %s", (session_id, user_id))
            > 0
        )

    def set_title(self, session_id: str, user_id: str, title: str) -> None:
        self._exec(
            f"UPDATE {self.s}.sessions SET title = %s WHERE session_id = %s AND user_id = %s",
            (title, session_id, user_id),
        )

    def recent_turns(self, session_id: str, user_id: str, turns: int) -> list[dict]:
        if turns <= 0 or not is_uuid(session_id):
            return []
        return self._rows(
            f"SELECT m.turn_index, m.role, m.content FROM {self.s}.messages m JOIN {self.s}.sessions s "
            f"USING (session_id) WHERE m.session_id = %s AND s.user_id = %s AND m.turn_index > s.turn_count - %s "
            f"ORDER BY m.turn_index, CASE m.role WHEN 'user' THEN 0 ELSE 1 END",
            (session_id, user_id, turns),
        )

    def messages(self, session_id: str, user_id: str) -> list[dict]:
        if not is_uuid(session_id):
            return []
        return self._rows(
            f"SELECT m.* FROM {self.s}.messages m JOIN {self.s}.sessions s USING (session_id) "
            f"WHERE m.session_id = %s AND s.user_id = %s ORDER BY m.turn_index, "
            f"CASE m.role WHEN 'user' THEN 0 ELSE 1 END",
            (session_id, user_id),
        )

    def find_turn_by_client_id(self, session_id: str, user_id: str, client_message_id: str) -> dict | None:
        rows = self._rows(
            f"SELECT a.* FROM {self.s}.messages u JOIN {self.s}.messages a ON a.session_id = u.session_id "
            f"AND a.turn_index = u.turn_index AND a.role = 'assistant' JOIN {self.s}.sessions s "
            f"ON s.session_id = u.session_id WHERE u.session_id = %s AND s.user_id = %s AND u.client_message_id = %s",
            (session_id, user_id, client_message_id),
        )
        return rows[0] if rows else None

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
        with self.pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                f"SELECT turn_count FROM {self.s}.sessions WHERE session_id = %s AND user_id = %s FOR UPDATE",
                (session_id, user_id),
            )
            row = cur.fetchone()
            if row is None:
                raise PermissionError("session not found for this user")
            turn = row[0] + 1
            cur.execute(
                f"INSERT INTO {self.s}.messages (message_id, session_id, turn_index, role, content, "
                f"client_message_id) VALUES (%s, %s, %s, 'user', %s, %s)",
                (uuid7(), session_id, turn, question, client_message_id),
            )
            cur.execute(
                f"INSERT INTO {self.s}.messages (message_id, session_id, turn_index, role, content, "
                f"standalone_query, citations, request_id, latency_ms) "
                f"VALUES (%s, %s, %s, 'assistant', %s, %s, %s, %s, %s)",
                (uuid7(), session_id, turn, answer, standalone_query, json.dumps(citations), request_id, latency_ms),
            )
            cur.execute(
                f"UPDATE {self.s}.sessions SET turn_count = %s, last_active_at = now() WHERE session_id = %s",
                (turn, session_id),
            )
            return turn

    def add_feedback(self, user_id: str, request_id: str, rating: int, comment: str | None) -> None:
        self._exec(
            f"INSERT INTO {self.s}.feedback (feedback_id, user_id, request_id, rating, comment) "
            f"VALUES (%s, %s, %s, %s, %s)",
            (uuid7(), user_id, request_id, rating, comment),
        )
