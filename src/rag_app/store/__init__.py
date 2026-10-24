"""Conversation and memory stores: Lakebase/Postgres (the real store) and in-memory SQLite (tests)."""

from __future__ import annotations

from rag_app.config import Settings
from rag_app.store.base import ConversationStore


def build_store(settings: Settings, workspace_client=None) -> ConversationStore:
    if settings.store_backend == "postgres":
        from rag_app.store.postgres import PostgresStore, make_pool

        store = PostgresStore(make_pool(workspace_client, settings.lakebase_instance), settings.pg_schema)
    else:
        from rag_app.store.sqlite import SQLiteStore

        store = SQLiteStore()
    if settings.auto_migrate or settings.store_backend == "memory":
        store.ensure_schema()
    return store
