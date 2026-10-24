"""Lakebase (Postgres) store: conversation history, long-term memory and feedback.

Authentication: Lakebase accepts a short-lived Databricks OAuth credential as the Postgres
password. Every new physical connection requests a fresh one, and pooled connections are
recycled before it expires. Locally the Postgres user is your Databricks user (from your
PAT/profile); in Databricks Apps the attached database resource sets PGHOST / PGUSER /
PGDATABASE for the app's service principal.

Schema changes are versioned SQL files in store/migrations/, applied in order and recorded
in {schema}.schema_migrations.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from pathlib import Path

from rag_app.store.base import is_uuid, normalize_fact, uuid7

log = logging.getLogger(__name__)
MIGRATIONS = Path(__file__).parent / "migrations"


def make_pool(workspace_client, instance_name: str, max_size: int = 8):
    import psycopg
    from psycopg_pool import ConnectionPool

    w = workspace_client
    host = os.getenv("PGHOST") or w.database.get_database_instance(instance_name).read_write_dns
    user = os.getenv("PGUSER") or w.current_user.me().user_name
    dbname = os.getenv("PGDATABASE", "databricks_postgres")

    class TokenConnection(psycopg.Connection):
        @classmethod
        def connect(cls, conninfo: str = "", **kwargs):
            cred = w.database.generate_database_credential(request_id=str(uuid.uuid4()), instance_names=[instance_name])
            kwargs["password"] = cred.token
            return super().connect(conninfo, **kwargs)

    conninfo = f"host={host} port={os.getenv('PGPORT', '5432')} dbname={dbname} user={user} sslmode=require"
    log.info("connecting to Lakebase instance=%s host=%s db=%s user=%s", instance_name, host, dbname, user)
    return ConnectionPool(
        conninfo,
        connection_class=TokenConnection,
        min_size=1,
        max_size=max_size,
        max_lifetime=45 * 60,  # credentials last ~1 hour
        kwargs={"autocommit": True},
        open=True,
    )


class PostgresStore:
    def __init__(self, pool, schema: str = "rag_memory"):
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
            raise ValueError("invalid schema name")
        self.pool, self.s = pool, schema

    # -- helpers ------------------------------------------------------------------
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

    # -- schema ---------------------------------------------------------------------
    def ensure_schema(self) -> list[str]:
        """Apply pending migrations in order; returns the ones applied now."""
        applied_now = []
        with self.pool.connection() as conn:
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {self.s}")
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {self.s}.schema_migrations "
                f"(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            done = {r[0] for r in conn.execute(f"SELECT version FROM {self.s}.schema_migrations").fetchall()}
            for path in sorted(MIGRATIONS.glob("*.sql")):
                if path.stem in done:
                    continue
                with conn.transaction():
                    conn.execute(path.read_text().replace("{schema}", self.s))
                    conn.execute(f"INSERT INTO {self.s}.schema_migrations (version) VALUES (%s)", (path.stem,))
                applied_now.append(path.stem)
                log.info("applied migration %s to schema %s", path.stem, self.s)
        return applied_now

    # -- sessions ----------------------------------------------------------------------
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

    def update_summary(self, session_id: str, user_id: str, summary: str, upto_turn: int) -> None:
        # only moves forward: a slower concurrent summarizer can never overwrite a newer summary
        self._exec(
            f"UPDATE {self.s}.sessions SET running_summary = %s, summary_upto_turn = %s "
            f"WHERE session_id = %s AND user_id = %s AND summary_upto_turn < %s",
            (summary, upto_turn, session_id, user_id, upto_turn),
        )

    # -- messages ------------------------------------------------------------------------
    def turns_between(self, session_id: str, user_id: str, after_turn: int, upto_turn: int | None = None) -> list[dict]:
        if not is_uuid(session_id):
            return []
        return self._rows(
            f"SELECT m.turn_index, m.role, m.content FROM {self.s}.messages m JOIN {self.s}.sessions s "
            f"USING (session_id) WHERE m.session_id = %s AND s.user_id = %s AND m.turn_index > %s "
            f"AND (%s::int IS NULL OR m.turn_index <= %s::int) "
            f"ORDER BY m.turn_index, CASE m.role WHEN 'user' THEN 0 ELSE 1 END",
            (session_id, user_id, after_turn, upto_turn, upto_turn),
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
        # The row lock serializes concurrent turns on one session (e.g. two browser tabs).
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
                f"INSERT INTO {self.s}.messages (message_id, session_id, turn_index, role, content, client_message_id) "
                f"VALUES (%s, %s, %s, 'user', %s, %s)",
                (uuid7(), session_id, turn, question, client_message_id),
            )
            cur.execute(
                f"INSERT INTO {self.s}.messages (message_id, session_id, turn_index, role, content, standalone_query, "
                f"citations, request_id, latency_ms) VALUES (%s, %s, %s, 'assistant', %s, %s, %s, %s, %s)",
                (uuid7(), session_id, turn, answer, standalone_query, json.dumps(citations), request_id, latency_ms),
            )
            cur.execute(
                f"UPDATE {self.s}.sessions SET turn_count = %s, last_active_at = now() WHERE session_id = %s",
                (turn, session_id),
            )
            return turn

    # -- long-term memory ---------------------------------------------------------------------
    def list_memories(self, user_id: str, limit: int = 50) -> list[dict]:
        return self._rows(
            f"SELECT memory_id, memory_type, content, created_at, updated_at, last_used_at FROM {self.s}.user_memories "
            f"WHERE user_id = %s AND status = 'active' ORDER BY updated_at DESC LIMIT %s",
            (user_id, limit),
        )

    def add_memories(self, user_id: str, items: list[dict], session_id: str | None) -> int:
        added = 0
        for item in items:
            added += self._exec(
                f"INSERT INTO {self.s}.user_memories (memory_id, user_id, memory_type, content, normalized, "
                f"source_session_id) VALUES (%s, %s, %s, %s, %s, %s) "
                f"ON CONFLICT (user_id, normalized) WHERE status = 'active' DO NOTHING",
                (uuid7(), user_id, item["type"], item["content"], normalize_fact(item["content"]), session_id),
            )
        return added

    def delete_memory(self, user_id: str, memory_id: str | None = None) -> int:
        if memory_id is not None and not is_uuid(memory_id):
            return 0
        # Hard delete: "forget" must remove the text, not just hide it.
        return self._exec(
            f"DELETE FROM {self.s}.user_memories WHERE user_id = %s AND (%s::uuid IS NULL OR memory_id = %s::uuid)",
            (user_id, memory_id, memory_id),
        )

    def touch_memories(self, user_id: str, memory_ids: list[str]) -> None:
        ids = [m for m in memory_ids if is_uuid(m)]
        if ids:
            self._exec(
                f"UPDATE {self.s}.user_memories SET last_used_at = now() WHERE user_id = %s AND memory_id = ANY(%s::uuid[])",
                (user_id, ids),
            )

    # -- feedback --------------------------------------------------------------------------------
    def add_feedback(self, user_id: str, request_id: str, rating: int, comment: str | None) -> None:
        self._exec(
            f"INSERT INTO {self.s}.feedback (feedback_id, user_id, request_id, rating, comment) "
            f"VALUES (%s, %s, %s, %s, %s)",
            (uuid7(), user_id, request_id, rating, comment),
        )
