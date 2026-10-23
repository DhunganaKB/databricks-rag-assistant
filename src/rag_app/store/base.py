"""Conversation store interface.

Implementations: SQLiteStore (local development, tests) and PostgresStore (Lakebase in
Databricks). Every read and write is scoped by user_id, so one user can never read or
write another user's session, even with a guessed session_id.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Protocol


def uuid7() -> str:
    """Time-ordered UUID (v7): sessions and messages sort by creation time."""
    ms = int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(10), "big")
    value = (
        (ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | ((rand >> 62) & 0xFFF) << 64 | 0b10 << 62 | (rand & ((1 << 62) - 1))
    )
    return str(uuid.UUID(int=value))


def is_uuid(value: str | None) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (TypeError, ValueError):
        return False


class ConversationStore(Protocol):
    def ensure_schema(self) -> None: ...

    def create_session(self, user_id: str) -> dict: ...

    def get_session(self, session_id: str, user_id: str) -> dict | None: ...

    def list_sessions(self, user_id: str, limit: int = 50) -> list[dict]: ...

    def delete_session(self, session_id: str, user_id: str) -> bool: ...

    def set_title(self, session_id: str, user_id: str, title: str) -> None: ...

    def recent_turns(self, session_id: str, user_id: str, turns: int) -> list[dict]:
        """The last `turns` question/answer pairs, oldest first: [{"role", "content", "turn_index"}]."""
        ...

    def messages(self, session_id: str, user_id: str) -> list[dict]: ...

    def find_turn_by_client_id(self, session_id: str, user_id: str, client_message_id: str) -> dict | None: ...

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
    ) -> int: ...

    def add_feedback(self, user_id: str, request_id: str, rating: int, comment: str | None) -> None: ...
