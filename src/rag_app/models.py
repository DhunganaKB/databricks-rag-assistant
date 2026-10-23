"""Domain and API models. Citations carry their source locations so any client can draw
the box (paged documents) or highlight the span (text documents)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Box(BaseModel):
    """A highlight rectangle on a rendered page. Coordinates are fractions (0..1) of the
    page width/height with the origin at the top-left corner; pages are 1-based."""

    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    estimated: bool = False


class TextSpan(BaseModel):
    """A highlight range in a text document: [char_start, char_end) into the document's
    source_text, plus 1-based inclusive line numbers."""

    char_start: int
    char_end: int
    line_start: int | None = None
    line_end: int | None = None


class Citation(BaseModel):
    n: int = Field(description="Number used in the answer text, e.g. [2]")
    chunk_id: str
    doc_id: str
    title: str
    section: str = ""
    path: str = ""
    doc_format: str = Field(description="paged (boxes) | text (span)")
    pages: list[int] = []
    boxes: list[Box] = []
    span: TextSpan | None = None
    snippet: str = ""


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(default=None, max_length=64)
    client_message_id: str | None = Field(default=None, max_length=100)


class ChatResponse(BaseModel):
    session_id: str
    request_id: str
    turn_index: int
    answer: str
    citations: list[Citation]
    standalone_query: str
    history_turns_used: int
    replayed: bool = False
    latency_ms: int | None = None


class FeedbackRequest(BaseModel):
    request_id: str = Field(max_length=64)
    rating: int = Field(ge=-1, le=1)
    comment: str | None = Field(default=None, max_length=2000)


class PageRegions(BaseModel):
    page: int
    image_url: str
    width_px: int | None = None
    height_px: int | None = None
    boxes: list[Box]


class CitationDetail(BaseModel):
    """Everything a viewer needs to render one citation."""

    chunk_id: str
    doc_id: str
    title: str
    doc_format: str
    pages: list[PageRegions] = []
    span: TextSpan | None = None
    text_url: str | None = None
