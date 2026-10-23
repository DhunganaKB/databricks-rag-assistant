"""Conversation stores: SQLite (local) and Postgres/Lakebase (Databricks)."""

from __future__ import annotations

from rag_app.config import Settings
from rag_app.store.base import ConversationStore


def build_store(settings: Settings, workspace_client=None) -> ConversationStore:
    if settings.store_backend == "postgres":
        from rag_app.store.postgres import PostgresStore, make_pool

        if not settings.lakebase_instance:
            raise ValueError("LAKEBASE_INSTANCE is required for STORE_BACKEND=postgres")
        store = PostgresStore(make_pool(workspace_client, settings.lakebase_instance), settings.pg_schema)
    else:
        from rag_app.store.sqlite import SQLiteStore

        store = SQLiteStore(settings.sqlite_path)
    store.ensure_schema()
    return store
