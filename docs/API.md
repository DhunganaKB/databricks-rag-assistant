# HTTP API

Base URL: `http://localhost:8000` locally; the Databricks App URL when deployed.

**Identity.** In Databricks Apps the platform authenticates the user and sends `X-Forwarded-Email`. The app uses only that header, never a user id from the request body. Locally, `LOCAL_USER_ID` from `.env` is used (tests and the smoke test send `x-forwarded-email` themselves). Every response carries `x-request-id`.

## Chat

### `POST /api/chat`
```json
{ "message": "What was revenue in Q3 2026?", "session_id": null, "client_message_id": "8b0c…" }
```
- `session_id`: `null` starts a new conversation; otherwise it must belong to the caller (404 if not).
- `client_message_id`: optional idempotency key. A retry with the same id returns the stored answer (`replayed: true`) instead of creating a new turn.

Response:
```json
{
  "session_id": "0199…", "request_id": "0199…", "turn_index": 2,
  "answer": "Revenue was 48.6 million USD [1] …",
  "standalone_query": "Revenue in Q3 2026 compared with Q3 2025",
  "history_turns_used": 1,
  "citations": [
    { "n": 1, "chunk_id": "347d…", "doc_id": "2951…", "title": "Quarterly Business Review - Q3 2026",
      "section": "Quarterly Business Review - Q3 2026 > 2. Financial results", "path": "/Volumes/…/qbr.pdf",
      "doc_format": "paged", "pages": [1],
      "boxes": [ { "page": 1, "x0": 0.13, "y0": 0.2959, "x1": 0.6409, "y1": 0.4472, "estimated": false } ],
      "span": null, "snippet": "| Metric | Q3 2025 | …" },
    { "n": 2, "doc_format": "text", "pages": [], "boxes": [],
      "span": { "char_start": 421, "char_end": 1285, "line_start": 11, "line_end": 29 }, "…": "…" }
  ],
  "replayed": false, "latency_ms": 3412
}
```

**Box coordinates** are fractions of the page width/height (0..1), origin top-left, pages 1-based. To draw on an image of width W and height H: `(x0·W, y0·H) – (x1·W, y1·H)`. `estimated: true` marks a box narrowed to part of a long paragraph.
**Span** is `[char_start, char_end)` into the document's `source_text` (see `/api/documents/{doc_id}/text`).

## Conversations
| Method | Path | Result |
|---|---|---|
| GET | `/api/sessions` | caller's sessions, newest first (`session_id, title, turn_count, …`) |
| GET | `/api/sessions/{session_id}/messages` | all turns incl. stored citations (with boxes) |
| DELETE | `/api/sessions/{session_id}` | deletes the session and its messages |
| POST | `/api/feedback` | `{"request_id": "…", "rating": 1 or -1, "comment": "…"}` |

## Citation viewer
| Method | Path | Result |
|---|---|---|
| GET | `/api/citations/{chunk_id}` | `{doc_format, pages: [{page, image_url, width_px, height_px, boxes}], span, text_url}` |
| GET | `/api/citations/{chunk_id}/pages/{page}/annotated.png` | the page with the chunk's boxes drawn in (PNG) |
| GET | `/api/documents/{doc_id}/pages/{page}/image` | rendered page image (JPEG) |
| GET | `/api/documents/{doc_id}/text` | `{doc_id, title, path, source_text}` for text documents |

## Operations
| Method | Path | Result |
|---|---|---|
| GET | `/api/health` | status, version, index, model, history window, store backend |
| GET | `/api/me` | caller identity, assistant name, history window |

Errors: `400` invalid input or id, `401` no identity, `404` not found or not yours, `422` validation (e.g. empty or too long message), `500` with `request_id` for correlation in the logs.
