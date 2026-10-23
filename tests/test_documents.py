"""DocumentService against a fake workspace client: SQL is parameterized, results are
mapped into citation details, page images are only read from the page-image volume."""

import io
from types import SimpleNamespace

import pytest

from rag_app.documents import DocumentService, annotate_page
from rag_app.models import Box

from conftest import make_settings

SUCCEEDED = SimpleNamespace(value="SUCCEEDED")


class FakeStatements:
    def __init__(self, tables):
        self.tables, self.calls = tables, []

    def execute_statement(self, statement, warehouse_id, wait_timeout, parameters):
        from databricks.sdk.service.sql import StatementState

        params = {p.name: p.value for p in parameters}
        self.calls.append((statement, params))
        if "citation_locations" in statement and "chunk_id = :chunk_id" in statement:
            cols, rows = self.tables["citation"]
            rows = [r for r in rows if params["chunk_id"] == "chunk-1"]
        elif "image_path FROM" in statement:
            cols, rows = ["image_path"], [[self.tables["image_path"]]] if params["doc_id"] == "doc-1" else []
        else:
            cols, rows = self.tables["doc"]
            rows = rows if params["doc_id"] == "doc-txt" else []
        return SimpleNamespace(
            status=SimpleNamespace(state=StatementState.SUCCEEDED, error=None),
            manifest=SimpleNamespace(schema=SimpleNamespace(columns=[SimpleNamespace(name=c) for c in cols])),
            result=SimpleNamespace(data_array=rows),
        )


CIT_COLS = [
    "doc_id",
    "title",
    "doc_format",
    "location_type",
    "page",
    "width_px",
    "height_px",
    "x0",
    "y0",
    "x1",
    "y1",
    "estimated",
    "char_start",
    "char_end",
    "line_start",
    "line_end",
]


def make_service(image_path="/Volumes/cat/silver/page_images/abc.jpg", rows=None):
    rows = rows or [
        [
            "doc-1",
            "QBR",
            "paged",
            "box",
            "2",
            "1654",
            "2339",
            "0.1",
            "0.2",
            "0.9",
            "0.3",
            "false",
            None,
            None,
            None,
            None,
        ],
        [
            "doc-1",
            "QBR",
            "paged",
            "box",
            "1",
            "1654",
            "2339",
            "0.1",
            "0.5",
            "0.9",
            "0.6",
            "true",
            None,
            None,
            None,
            None,
        ],
    ]
    tables = {
        "citation": (CIT_COLS, rows),
        "image_path": image_path,
        "doc": (
            ["doc_id", "title", "path", "doc_format", "source_text"],
            [["doc-txt", "PTO", "/V/pto.txt", "text", "hello"]],
        ),
    }
    downloads = []
    w = SimpleNamespace(
        statement_execution=FakeStatements(tables),
        files=SimpleNamespace(download=lambda p: downloads.append(p) or SimpleNamespace(contents=io.BytesIO(b"JPEG"))),
    )
    return DocumentService(w, make_settings()), w, downloads


def test_citation_detail_groups_boxes_by_page_and_uses_parameters():
    svc, w, _ = make_service()
    d = svc.citation_detail("chunk-1")
    assert [p.page for p in d.pages] == [1, 2]
    assert d.pages[0].boxes[0].estimated is True and d.pages[1].boxes[0].y0 == pytest.approx(0.2)
    assert d.pages[0].image_url == "/api/documents/doc-1/pages/1/image"
    statement, params = w.statement_execution.calls[0]
    assert params == {"chunk_id": "chunk-1"} and "chunk-1" not in statement  # bound parameter, not interpolated
    svc.citation_detail("chunk-1")
    assert len(w.statement_execution.calls) == 1  # cached


def test_citation_detail_text_span():
    rows = [["doc-txt", "PTO", "text", "span", None, None, None, None, None, None, None, None, "10", "90", "3", "8"]]
    svc, _, _ = make_service(rows=rows)
    d = svc.citation_detail("chunk-1")
    assert d.pages == [] and d.span.char_start == 10 and d.span.line_end == 8
    assert d.text_url == "/api/documents/doc-txt/text"


def test_unknown_citation_and_document():
    svc, _, _ = make_service()
    assert svc.citation_detail("chunk-404") is None
    assert svc.document_text("nope") is None
    assert svc.document_text("doc-txt")["source_text"] == "hello"


def test_page_image_only_from_page_volume():
    svc, _, downloads = make_service()
    assert svc.page_image("doc-1", 1) == b"JPEG" and downloads == ["/Volumes/cat/silver/page_images/abc.jpg"]
    svc_bad, _, downloads_bad = make_service(image_path="/Volumes/cat/secret/payroll.jpg")
    assert svc_bad.page_image("doc-1", 1) is None and downloads_bad == []  # path guard
    assert svc.page_image("doc-404", 1) is None


def test_missing_warehouse_is_a_clear_error():
    svc, _, _ = make_service()
    svc.s = make_settings(sql_warehouse_id="")
    with pytest.raises(RuntimeError, match="SQL_WAREHOUSE_ID"):
        svc.citation_detail("chunk-2")


def test_annotate_page_draws_box():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (100, 200), "white").save(buf, format="PNG")
    out = Image.open(io.BytesIO(annotate_page(buf.getvalue(), [Box(page=1, x0=0.1, y0=0.1, x1=0.5, y1=0.5)])))
    assert out.getpixel((10, 40)) != (255, 255, 255) and out.getpixel((90, 190)) == (255, 255, 255)
