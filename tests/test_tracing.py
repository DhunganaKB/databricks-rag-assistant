import pytest
from fastapi.testclient import TestClient

from rag_app import tracing
from rag_app.agent import RagAgent
from rag_app.api import create_app
from rag_app.store.sqlite import SQLiteStore

from conftest import FakeDocuments, FakeLLM, FakeRetriever, make_settings

ALICE = {"x-forwarded-email": "alice@example.com"}
BOB = {"x-forwarded-email": "bob@example.com"}


@pytest.fixture
def traced(monkeypatch):
    """Tracing 'on' without a tracking server: the active trace id is a fixed value and
    feedback calls are recorded instead of sent to MLflow."""
    sent = []
    monkeypatch.setattr(tracing, "_enabled", True)
    monkeypatch.setattr(tracing.mlflow, "get_active_trace_id", lambda: "tr-123")
    monkeypatch.setattr(tracing.mlflow, "update_current_trace", lambda **kw: sent.append(("tag", kw)))
    monkeypatch.setattr(tracing.mlflow, "log_feedback", lambda **kw: sent.append(("feedback", kw)))
    return sent


@pytest.fixture
def client():
    settings = make_settings(local_user_id="")
    store = SQLiteStore()
    store.ensure_schema()
    with TestClient(create_app(settings, RagAgent(settings, store, FakeRetriever(), FakeLLM()), FakeDocuments())) as c:
        yield c, store


def test_off_without_experiment():
    assert tracing.setup("") is False
    assert not tracing.enabled() and tracing.current_trace_id() is None
    tracing.tag_trace(user="u", session_id="s")  # no-op, no error
    tracing.log_user_feedback("tr-1", "u", 1, None)


def test_turn_stores_trace_id_and_tags_trace(traced, client):
    c, store = client
    r = c.post("/api/chat", json={"message": "What was revenue?"}, headers=ALICE).json()
    assert store._q("SELECT trace_id FROM messages WHERE request_id = ?", (r["request_id"],)) == [
        {"trace_id": "tr-123"}
    ]
    tags = [kw for kind, kw in traced if kind == "tag"]
    assert tags[0]["user"] == "alice@example.com" and tags[0]["session_id"] == r["session_id"]
    assert tags[0]["client_request_id"] == r["request_id"]
    assert c.get("/api/health").json()["tracing"] is True


def test_feedback_is_attached_to_the_answers_trace(traced, client):
    c, _ = client
    rid = c.post("/api/chat", json={"message": "What was revenue?"}, headers=ALICE).json()["request_id"]
    assert c.post("/api/feedback", json={"request_id": rid, "rating": -1, "comment": "wrong year"}, headers=ALICE)
    (fb,) = [kw for kind, kw in traced if kind == "feedback"]
    assert fb["trace_id"] == "tr-123" and fb["value"] is False and fb["rationale"] == "wrong year"
    assert fb["source"].source_type == "HUMAN" and fb["source"].source_id == "alice@example.com"


def test_feedback_on_someone_elses_answer_is_not_linked(traced, client):
    c, store = client
    rid = c.post("/api/chat", json={"message": "What was revenue?"}, headers=ALICE).json()["request_id"]
    assert c.post("/api/feedback", json={"request_id": rid, "rating": 1}, headers=BOB).json() == {"ok": True}
    assert not [kw for kind, kw in traced if kind == "feedback"]
    assert store.add_feedback("bob@example.com", rid, 1, None) is None
    assert store.add_feedback("alice@example.com", rid, 1, None) == "tr-123"


def test_tracing_errors_never_fail_a_request(traced, client, monkeypatch):
    def boom(**_):
        raise RuntimeError("tracking server down")

    monkeypatch.setattr(tracing.mlflow, "update_current_trace", boom)
    monkeypatch.setattr(tracing.mlflow, "log_feedback", boom)
    c, _ = client
    rid = c.post("/api/chat", json={"message": "What was revenue?"}, headers=ALICE).json()["request_id"]
    assert c.post("/api/feedback", json={"request_id": rid, "rating": 1}, headers=ALICE).status_code == 200


def test_feedback_waits_for_a_trace_that_is_still_being_exported(traced, monkeypatch):
    calls = []

    def log_feedback(**kw):
        calls.append(kw)
        if len(calls) < 3:
            raise RuntimeError("NOT_FOUND: Trace with ID tr-123 not found.")

    monkeypatch.setattr(tracing.mlflow, "log_feedback", log_feedback)
    tracing.log_user_feedback("tr-123", "alice@example.com", 1, None, base_delay=0)
    assert len(calls) == 3 and calls[-1]["value"] is True
