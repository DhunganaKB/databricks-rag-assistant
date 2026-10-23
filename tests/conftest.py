"""Shared fakes: no network, no workspace. The real Databricks clients are replaced by
small deterministic doubles with the same interfaces."""

from __future__ import annotations

import io

import pytest

from rag_app.agent import RagAgent
from rag_app.config import Settings
from rag_app.models import Box, CitationDetail, PageRegions, TextSpan
from rag_app.retriever import Hit
from rag_app.store.sqlite import SQLiteStore


def make_settings(**overrides) -> Settings:
    base = dict(
        vs_index="cat.gold.chunks_index",
        citations_view="cat.gold.citation_locations",
        documents_view="cat.gold.rag_documents",
        page_image_root="/Volumes/cat/silver/page_images/",
        sql_warehouse_id="wh",
        history_turns=5,
        sqlite_path=":memory:",
        local_user_id="alice@example.com",
    )
    base.update(overrides)
    return Settings(**base)


PDF_HIT = Hit(
    chunk_id="chunk-pdf-0001",
    doc_id="doc-pdf-0001",
    title="Quarterly Business Review - Q3 2026",
    section_path="Quarterly Business Review - Q3 2026 > 2. Financial results",
    path="/Volumes/x/qbr.pdf",
    doc_format="paged",
    text="| Metric | Q3 2025 | Q3 2026 |\n| Revenue | 37.1 | 48.6 |",
    score=0.99,
    boxes=[Box(page=1, x0=0.13, y0=0.30, x1=0.64, y1=0.45)],
)
TXT_HIT = Hit(
    chunk_id="chunk-txt-0001",
    doc_id="doc-txt-0001",
    title="Paid Time Off and Leave Policy",
    section_path="Vacation",
    path="/Volumes/x/pto.txt",
    doc_format="text",
    text="| 2 to 5 years | 22 days | 5 days |",
    score=0.95,
    span=TextSpan(char_start=120, char_end=480, line_start=11, line_end=30),
)


class FakeRetriever:
    def __init__(self, hits=None):
        self.hits = hits if hits is not None else [PDF_HIT, TXT_HIT]
        self.queries: list[str] = []

    def search(self, query, num_results):
        self.queries.append(query)
        return self.hits[:num_results]


class FakeLLM:
    """Routes on prompt content; records every call so tests can inspect the prompts."""

    def __init__(self, answer="Revenue was 48.6 million USD [1]. Employees get 22 days [2]."):
        self.answer = answer
        self.calls: list[tuple[str | None, list[dict]]] = []

    def complete(self, messages, *, max_tokens=800, temperature=0.1, endpoint=None):
        self.calls.append((endpoint, messages))
        last = messages[-1]["content"]
        if "Standalone query:" in last:
            return "standalone: " + last.rsplit("Latest message:", 1)[1].split("\n")[0].strip()
        if "3 to 6 word title" in last:
            return "Revenue And Vacation"
        return self.answer

    def answer_calls(self):
        return [m for e, m in self.calls if e is None]


def _jpeg(w=200, h=280) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, format="JPEG")
    return buf.getvalue()


class FakeDocuments:
    def citation_detail(self, chunk_id):
        if chunk_id == PDF_HIT.chunk_id:
            return CitationDetail(
                chunk_id=chunk_id,
                doc_id=PDF_HIT.doc_id,
                title=PDF_HIT.title,
                doc_format="paged",
                pages=[
                    PageRegions(
                        page=1,
                        image_url=f"/api/documents/{PDF_HIT.doc_id}/pages/1/image",
                        width_px=200,
                        height_px=280,
                        boxes=PDF_HIT.boxes,
                    )
                ],
            )
        if chunk_id == TXT_HIT.chunk_id:
            return CitationDetail(
                chunk_id=chunk_id,
                doc_id=TXT_HIT.doc_id,
                title=TXT_HIT.title,
                doc_format="text",
                span=TXT_HIT.span,
                text_url=f"/api/documents/{TXT_HIT.doc_id}/text",
            )
        return None

    def page_image(self, doc_id, page):
        return _jpeg() if doc_id == PDF_HIT.doc_id and page == 1 else None

    def document_text(self, doc_id):
        if doc_id == TXT_HIT.doc_id:
            return {"doc_id": doc_id, "title": TXT_HIT.title, "path": TXT_HIT.path, "source_text": "x" * 600}
        return None


@pytest.fixture
def settings():
    return make_settings()


@pytest.fixture
def store():
    s = SQLiteStore(":memory:")
    s.ensure_schema()
    return s


@pytest.fixture
def agent(settings, store):
    return RagAgent(settings, store, FakeRetriever(), FakeLLM())
