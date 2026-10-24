"""Conversation and memory store interface.

  short-term memory   sessions + messages (last N turns verbatim) + sessions.running_summary
  long-term memory    user_memories: durable facts about a user, across sessions
  quality loop        feedback

Implementations: PostgresStore (Lakebase in Databricks: the real store, used locally and
deployed) and SQLiteStore (in-memory only, for unit tests). Every read and write is scoped
by user_id, so one user can never read or change another user's data.
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


def normalize_fact(text: str) -> str:
    """Key used to de-duplicate memories ("User is in HR." == "user is in hr")."""
    return " ".join(text.lower().split()).rstrip(".")


class ConversationStore(Protocol):
    def ensure_schema(self) -> None: ...

    # sessions
    def create_session(self, user_id: str) -> dict: ...

    def get_session(self, session_id: str, user_id: str) -> dict | None:
        """Includes turn_count, running_summary and summary_upto_turn."""
        ...

    def list_sessions(self, user_id: str, limit: int = 50) -> list[dict]: ...

    def delete_session(self, session_id: str, user_id: str) -> bool: ...

    def set_title(self, session_id: str, user_id: str, title: str) -> None: ...

    def update_summary(self, session_id: str, user_id: str, summary: str, upto_turn: int) -> None: ...

    # messages (short-term memory)
    def turns_between(self, session_id: str, user_id: str, after_turn: int, upto_turn: int | None = None) -> list[dict]:
        """Messages with after_turn < turn_index <= upto_turn, oldest first: [{turn_index, role, content}]."""
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

    # long-term memory
    def list_memories(self, user_id: str, limit: int = 50) -> list[dict]: ...

    def add_memories(self, user_id: str, items: list[dict], session_id: str | None) -> int:
        """items: [{"type": "profile"|"preference", "content": str}]; duplicates are skipped."""
        ...

    def delete_memory(self, user_id: str, memory_id: str | None = None) -> int:
        """Delete one memory, or all of the user's memories when memory_id is None."""
        ...

    def touch_memories(self, user_id: str, memory_ids: list[str]) -> None: ...

    # feedback
    def add_feedback(self, user_id: str, request_id: str, rating: int, comment: str | None) -> None: ...
