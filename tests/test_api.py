import io

import pytest
from fastapi.testclient import TestClient

from rag_app.agent import RagAgent
from rag_app.api import create_app
from rag_app.store.sqlite import SQLiteStore

from conftest import PDF_HIT, TXT_HIT, FakeDocuments, FakeLLM, FakeRetriever, make_settings

ALICE = {"x-forwarded-email": "Alice@Example.com"}
BOB = {"x-forwarded-email": "bob@example.com"}


@pytest.fixture
def client():
    settings = make_settings(local_user_id="")
    store = SQLiteStore(":memory:")
    store.ensure_schema()
    agent = RagAgent(settings, store, FakeRetriever(), FakeLLM())
    with TestClient(create_app(settings, agent, FakeDocuments())) as c:
        yield c


def test_health_and_ui(client):
    assert client.get("/api/health").json()["history_turns"] == 5
    page = client.get("/")
    assert page.status_code == 200 and "Knowledge Assistant" in page.text
    assert client.get("/static/app.js").status_code == 200


def test_requires_identity(client):
    assert client.post("/api/chat", json={"message": "hi"}).status_code == 401


def test_chat_returns_citations_with_boxes_and_spans(client):
    r = client.post("/api/chat", json={"message": "What was revenue?", "client_message_id": "m1"}, headers=ALICE)
    assert r.status_code == 200 and r.headers["x-request-id"]
    body = r.json()
    pdf, txt = body["citations"]
    assert pdf["boxes"][0] == {"page": 1, "x0": 0.13, "y0": 0.30, "x1": 0.64, "y1": 0.45, "estimated": False}
    assert txt["span"]["char_start"] == 120
    sessions = client.get("/api/sessions", headers=ALICE).json()
    assert len(sessions) == 1 and sessions[0]["title"] == "Revenue And Vacation"  # background title task
    messages = client.get(f"/api/sessions/{body['session_id']}/messages", headers=ALICE).json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["citations"][0]["boxes"][0]["page"] == 1


def test_sessions_are_private(client):
    sid = client.post("/api/chat", json={"message": "hi"}, headers=ALICE).json()["session_id"]
    assert client.get(f"/api/sessions/{sid}/messages", headers=BOB).status_code == 404
    assert client.post("/api/chat", json={"message": "x", "session_id": sid}, headers=BOB).status_code == 404
    assert client.delete(f"/api/sessions/{sid}", headers=BOB).status_code == 404
    assert client.get("/api/sessions", headers=BOB).json() == []
    assert client.delete(f"/api/sessions/{sid}", headers=ALICE).status_code == 200


def test_input_validation(client):
    assert client.post("/api/chat", json={"message": ""}, headers=ALICE).status_code == 422
    assert client.post("/api/chat", json={"message": "x" * 4001}, headers=ALICE).status_code == 422
    assert client.post("/api/chat", json={"message": "x", "session_id": "../etc"}, headers=ALICE).status_code == 400
    assert client.get("/api/citations/bad id!", headers=ALICE).status_code in (400, 404)


def test_citation_detail_and_page_image(client):
    d = client.get(f"/api/citations/{PDF_HIT.chunk_id}", headers=ALICE).json()
    assert d["doc_format"] == "paged" and d["pages"][0]["boxes"][0]["x1"] == 0.64
    img = client.get(d["pages"][0]["image_url"], headers=ALICE)
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/citations/{'nope' * 3}", headers=ALICE).status_code == 404


def test_annotated_png_has_box_drawn(client):
    from PIL import Image

    r = client.get(f"/api/citations/{PDF_HIT.chunk_id}/pages/1/annotated.png", headers=ALICE)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    img = Image.open(io.BytesIO(r.content)).convert("RGB")
    w, h = img.size
    edge = img.getpixel((round(0.13 * w) + 1, round(0.375 * h)))  # left edge of the box
    inside_white = img.getpixel((5, 5))  # far from the box
    assert edge != (255, 255, 255) and inside_white == (255, 255, 255)
    assert client.get(f"/api/citations/{PDF_HIT.chunk_id}/pages/9/annotated.png", headers=ALICE).status_code == 404


def test_text_document(client):
    d = client.get(f"/api/citations/{TXT_HIT.chunk_id}", headers=ALICE).json()
    assert d["span"]["line_start"] == 11 and d["text_url"]
    doc = client.get(d["text_url"], headers=ALICE).json()
    assert len(doc["source_text"]) == 600


def test_feedback(client):
    rid = client.post("/api/chat", json={"message": "hi"}, headers=ALICE).json()["request_id"]
    assert client.post(
        "/api/feedback", json={"request_id": rid, "rating": -1, "comment": "wrong"}, headers=ALICE
    ).json() == {"ok": True}
    assert client.post("/api/feedback", json={"request_id": rid, "rating": 5}, headers=ALICE).status_code == 422
