"""Hybrid retrieval over the gold Vector Search index. Every hit already carries its
citation locations (synced by the ETL), so no extra lookup is needed to cite it."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Protocol

from rag_app.models import Box, TextSpan

log = logging.getLogger(__name__)

COLUMNS = [
    "chunk_id",
    "doc_id",
    "content_hash",
    "title",
    "section_path",
    "path",
    "doc_format",
    "page_start",
    "page_end",
    "chunk_text",
    "locations_json",
    "char_start",
    "char_end",
    "line_start",
    "line_end",
]


@dataclass
class Hit:
    chunk_id: str
    doc_id: str
    title: str
    section_path: str
    path: str
    doc_format: str
    text: str
    score: float = 0.0
    boxes: list[Box] = field(default_factory=list)
    span: TextSpan | None = None

    @property
    def pages(self) -> list[int]:
        return sorted({b.page for b in self.boxes})

    @property
    def section(self) -> str:
        """Section path without a leading document title ("QBR > 2. Results" -> "2. Results")."""
        s = self.section_path or ""
        if s == self.title:
            return ""
        return s[len(self.title) + 3 :] if self.title and s.startswith(self.title + " > ") else s

    def label(self) -> str:
        label = " > ".join(p for p in (self.title or "Untitled", self.section) if p)
        return f"{label} (page {', '.join(map(str, self.pages))})" if self.pages else label


class Retriever(Protocol):
    def search(self, query: str, num_results: int) -> list[Hit]: ...


def _int(value) -> int | None:
    if value in (None, "", "None", "null"):
        return None
    return int(float(value))


def parse_boxes(raw: str | None) -> list[Box]:
    """locations_json -> boxes; malformed entries are skipped, never fatal."""
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        log.warning("invalid locations_json")
        return []
    boxes = []
    for it in items if isinstance(items, list) else []:
        try:
            if None in (it.get("page"), it.get("x0"), it.get("y0"), it.get("x1"), it.get("y1")):
                continue
            boxes.append(
                Box(
                    page=int(it["page"]),
                    x0=float(it["x0"]),
                    y0=float(it["y0"]),
                    x1=float(it["x1"]),
                    y1=float(it["y1"]),
                    estimated=bool(it.get("estimated")),
                )
            )
        except (TypeError, ValueError, AttributeError):
            continue
    return boxes


def hit_from_row(row: dict) -> Hit:
    cs, ce = _int(row.get("char_start")), _int(row.get("char_end"))
    span = None
    if cs is not None and ce is not None:
        span = TextSpan(
            char_start=cs, char_end=ce, line_start=_int(row.get("line_start")), line_end=_int(row.get("line_end"))
        )
    return Hit(
        chunk_id=row.get("chunk_id") or "",
        doc_id=row.get("doc_id") or "",
        title=row.get("title") or "",
        section_path=row.get("section_path") or "",
        path=row.get("path") or "",
        doc_format=row.get("doc_format") or ("paged" if row.get("locations_json") not in (None, "[]") else "text"),
        text=row.get("chunk_text") or "",
        score=float(row.get("score") or 0.0),
        boxes=parse_boxes(row.get("locations_json")),
        span=span,
    )


def diversify(hits: list[Hit], limit: int, max_per_doc: int) -> list[Hit]:
    """Keep rank order but cap chunks per document so one long document can't fill the context."""
    out, per_doc = [], {}
    for hit in hits:
        if per_doc.get(hit.doc_id, 0) >= max_per_doc:
            continue
        per_doc[hit.doc_id] = per_doc.get(hit.doc_id, 0) + 1
        out.append(hit)
        if len(out) == limit:
            break
    return out


class VectorSearchRetriever:
    def __init__(self, workspace_client, index_name: str):
        self.w = workspace_client
        self.index_name = index_name

    def search(self, query: str, num_results: int) -> list[Hit]:
        resp = self.w.vector_search_indexes.query_index(
            index_name=self.index_name, columns=COLUMNS, query_text=query, num_results=num_results, query_type="HYBRID"
        )
        names = [c.name for c in resp.manifest.columns]
        rows = resp.result.data_array if resp.result and resp.result.data_array else []
        return [hit_from_row(dict(zip(names, r, strict=False))) for r in rows]
