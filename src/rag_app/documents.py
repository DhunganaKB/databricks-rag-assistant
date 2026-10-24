"""Citation viewer data: boxes + page images (paged documents) or source text (text documents).

Reads only the gold contract produced by the ETL project:
  citation_locations  one row per highlight region (box with page image, or text span)
  rag_documents       one row per document, incl. source_text for text documents

SQL runs on a SQL warehouse through the Statement Execution API with named parameters
(no string interpolation of user input). Page images are read through the Files API and
only from the configured page-image volume.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from collections import OrderedDict

from rag_app.config import Settings
from rag_app.models import Box, CitationDetail, PageRegions, TextSpan
from rag_app.retry import with_retry

log = logging.getLogger(__name__)


class _LRU:
    def __init__(self, size: int):
        self.size, self._d, self._lock = size, OrderedDict(), threading.Lock()

    def get(self, key):
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                return self._d[key]
        return None

    def put(self, key, value) -> None:
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self.size:
                self._d.popitem(last=False)


def annotate_page(image_bytes: bytes, boxes: list[Box], color: tuple[int, int, int] = (228, 87, 46)) -> bytes:
    """Draw citation boxes on a page image; returns PNG bytes."""
    from PIL import Image, ImageDraw

    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    w, h = img.size
    stroke = max(3, w // 400)
    for b in boxes:
        rect = [round(b.x0 * w), round(b.y0 * h), round(b.x1 * w), round(b.y1 * h)]
        draw.rectangle(rect, outline=color + (255,), width=stroke, fill=color + (40,))
    out = io.BytesIO()
    Image.alpha_composite(img, overlay).convert("RGB").save(out, format="PNG")
    return out.getvalue()


class DocumentService:
    def __init__(self, workspace_client, settings: Settings):
        self.w = workspace_client
        self.s = settings
        self._details = _LRU(512)
        self._texts = _LRU(64)
        self._images = _LRU(64)

    # -- SQL ------------------------------------------------------------------
    def _query(self, statement: str, params: dict[str, str]) -> list[dict]:
        from databricks.sdk.service.sql import StatementParameterListItem, StatementState

        if not self.s.sql_warehouse_id:
            raise RuntimeError("SQL_WAREHOUSE_ID is not configured")
        resp = with_retry(
            lambda: self.w.statement_execution.execute_statement(
                statement=statement,
                warehouse_id=self.s.sql_warehouse_id,
                wait_timeout="30s",
                parameters=[StatementParameterListItem(name=k, value=v, type="STRING") for k, v in params.items()],
            ),
            "SQL statement",
        )
        deadline = time.time() + 120
        while resp.status.state in (StatementState.PENDING, StatementState.RUNNING) and time.time() < deadline:
            time.sleep(1)
            resp = self.w.statement_execution.get_statement(resp.statement_id)
        if resp.status.state != StatementState.SUCCEEDED:
            raise RuntimeError(f"SQL failed: {resp.status.error.message if resp.status.error else resp.status.state}")
        cols = [c.name for c in resp.manifest.schema.columns]
        rows = resp.result.data_array if resp.result and resp.result.data_array else []
        return [dict(zip(cols, r, strict=False)) for r in rows]

    # -- citations --------------------------------------------------------------
    def citation_detail(self, chunk_id: str) -> CitationDetail | None:
        cached = self._details.get(chunk_id)
        if cached:
            return cached
        rows = self._query(
            f"SELECT doc_id, title, doc_format, location_type, page, width_px, height_px, x0, y0, x1, y1, estimated, "
            f"char_start, char_end, line_start, line_end FROM {self.s.citations_view} WHERE chunk_id = :chunk_id",
            {"chunk_id": chunk_id},
        )
        if not rows:
            return None
        first = rows[0]
        detail = CitationDetail(
            chunk_id=chunk_id, doc_id=first["doc_id"], title=first["title"] or "", doc_format=first["doc_format"] or ""
        )
        pages: dict[int, PageRegions] = {}
        for r in rows:
            if r["location_type"] == "box" and r["page"] is not None and r["x0"] is not None:
                page = int(r["page"])
                region = pages.setdefault(
                    page,
                    PageRegions(
                        page=page,
                        image_url=f"/api/documents/{detail.doc_id}/pages/{page}/image",
                        width_px=int(r["width_px"]) if r["width_px"] else None,
                        height_px=int(r["height_px"]) if r["height_px"] else None,
                        boxes=[],
                    ),
                )
                region.boxes.append(
                    Box(
                        page=page,
                        x0=float(r["x0"]),
                        y0=float(r["y0"]),
                        x1=float(r["x1"]),
                        y1=float(r["y1"]),
                        estimated=str(r["estimated"]).lower() == "true",
                    )
                )
            elif r["location_type"] == "span" and r["char_start"] is not None:
                detail.span = TextSpan(
                    char_start=int(r["char_start"]),
                    char_end=int(r["char_end"]),
                    line_start=int(r["line_start"]) if r["line_start"] else None,
                    line_end=int(r["line_end"]) if r["line_end"] else None,
                )
                detail.text_url = f"/api/documents/{detail.doc_id}/text"
        detail.pages = [pages[p] for p in sorted(pages)]
        self._details.put(chunk_id, detail)
        return detail

    # -- documents --------------------------------------------------------------
    def document_text(self, doc_id: str) -> dict | None:
        cached = self._texts.get(doc_id)
        if cached:
            return cached
        rows = self._query(
            f"SELECT doc_id, title, path, doc_format, source_text FROM {self.s.documents_view} WHERE doc_id = :doc_id",
            {"doc_id": doc_id},
        )
        if not rows:
            return None
        self._texts.put(doc_id, rows[0])
        return rows[0]

    def page_image(self, doc_id: str, page: int) -> bytes | None:
        key = (doc_id, page)
        cached = self._images.get(key)
        if cached:
            return cached
        rows = self._query(
            f"SELECT image_path FROM {self.s.citations_view} WHERE doc_id = :doc_id "
            f"AND page = CAST(:page AS INT) AND image_path IS NOT NULL LIMIT 1",
            {"doc_id": doc_id, "page": str(page)},
        )
        if not rows:
            return None
        path = rows[0]["image_path"]
        if not path.startswith(self.s.page_image_root):  # defense in depth: never read outside the volume
            log.warning("refusing image path outside page_image_root: %s", path)
            return None
        data = with_retry(lambda: self.w.files.download(path).contents.read(), "page image download")
        self._images.put(key, data)
        return data
