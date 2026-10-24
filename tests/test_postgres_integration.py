"""PostgresStore against a real Lakebase instance, in a throwaway schema.

Skipped unless RAG_INTEGRATION=1 (needs LAKEBASE_INSTANCE and Databricks credentials):

    RAG_INTEGRATION=1 uv run --env-file .env pytest -m integration
"""

import os

import pytest

from rag_app.store.postgres import PostgresStore, make_pool

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RAG_INTEGRATION") != "1", reason="set RAG_INTEGRATION=1 to run against Lakebase"),
]
SCHEMA = "rag_memory_it_test"


@pytest.fixture(scope="module")
def store():
    from databricks.sdk import WorkspaceClient

    pool = make_pool(WorkspaceClient(), os.environ["LAKEBASE_INSTANCE"], max_size=2)
    with pool.connection() as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    s = PostgresStore(pool, SCHEMA)
    assert s.ensure_schema() == ["001_initial"]
    assert s.ensure_schema() == []  # idempotent
    yield s
    with pool.connection() as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    pool.close()


def test_sessions_turns_and_isolation(store):
    sid = store.create_session("alice@example.com")["session_id"]
    for i in range(1, 4):
        turn = store.append_turn(
            sid,
            "alice@example.com",
            f"q{i}",
            f"a{i}",
            client_message_id=f"c{i}",
            standalone_query=f"q{i}",
            citations=[{"n": 1, "boxes": [{"page": 1, "x0": 0.1, "y0": 0.2, "x1": 0.3, "y1": 0.4}]}],
            request_id=f"r{i}",
            latency_ms=10,
        )
        assert turn == i
    assert [m["content"] for m in store.turns_between(sid, "alice@example.com", 1)] == ["q2", "a2", "q3", "a3"]
    assert store.find_turn_by_client_id(sid, "alice@example.com", "c2")["content"] == "a2"
    assert store.messages(sid, "alice@example.com")[1]["citations"][0]["boxes"][0]["y1"] == 0.4
    assert store.get_session(sid, "bob@example.com") is None
    with pytest.raises(PermissionError):
        store.append_turn(
            sid,
            "bob@example.com",
            "x",
            "y",
            client_message_id=None,
            standalone_query="",
            citations=[],
            request_id="r",
            latency_ms=1,
        )


def test_summary_only_moves_forward(store):
    sid = store.create_session("alice@example.com")["session_id"]
    store.update_summary(sid, "alice@example.com", "newer", 4)
    store.update_summary(sid, "alice@example.com", "stale", 2)
    s = store.get_session(sid, "alice@example.com")
    assert (s["running_summary"], s["summary_upto_turn"]) == ("newer", 4)


def test_long_term_memories(store):
    items = [{"type": "profile", "content": "User is in HR."}, {"type": "profile", "content": "user is in hr"}]
    assert store.add_memories("alice@example.com", items, None) == 1  # de-duplicated by ON CONFLICT
    mems = store.list_memories("alice@example.com")
    store.touch_memories("alice@example.com", [mems[0]["memory_id"]])
    assert store.list_memories("alice@example.com")[0]["last_used_at"] is not None
    assert store.list_memories("bob@example.com") == []
    assert store.delete_memory("bob@example.com", mems[0]["memory_id"]) == 0
    assert store.delete_memory("alice@example.com", None) == 1
    assert store.add_memories("alice@example.com", items[:1], None) == 1  # allowed again after delete


def test_delete_session_cascades(store):
    sid = store.create_session("carol@example.com")["session_id"]
    store.append_turn(
        sid,
        "carol@example.com",
        "q",
        "a",
        client_message_id=None,
        standalone_query="q",
        citations=[],
        request_id="r",
        latency_ms=1,
    )
    store.add_feedback("carol@example.com", "r", 1, "good")
    assert store.delete_session(sid, "carol@example.com") is True
    assert store.messages(sid, "carol@example.com") == []
