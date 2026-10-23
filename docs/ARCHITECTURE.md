# Architecture

## 1. Context
The **ETL** project (`../ETL`) turns documents into the gold layer this app reads. The app never touches bronze or raw data.

| Gold / silver object (dev names) | Used for |
|---|---|
| `steampunk.dev_kbdhunga_rag_gold.chunks_index` | hybrid retrieval; each hit includes `doc_format`, `locations_json` (boxes), `char_start/char_end/line_start/line_end` (spans) |
| `steampunk.dev_kbdhunga_rag_gold.citation_locations` | viewer: page image path, pixel size and boxes per chunk |
| `steampunk.dev_kbdhunga_rag_gold.rag_documents` | viewer: `source_text` of text documents |
| volume `steampunk.dev_kbdhunga_rag_silver.page_images` | rendered page JPEGs the boxes are drawn on |

## 2. Components
| Module | Responsibility |
|---|---|
| `api.py` | FastAPI app factory (`create_app` takes fakes in tests), routes, request-id + security-header middleware, identity from `X-Forwarded-Email` |
| `agent.py` | one turn: session → idempotency → history → rewrite → retrieve → generate → cite → persist |
| `retriever.py` | Vector Search `HYBRID` query, `hit_from_row` (string/NULL tolerant), `diversify` (max N chunks per document) |
| `llm.py` | OpenAI client on `<host>/serving-endpoints` with Databricks auth per request; adapts to endpoints that reject `temperature` |
| `documents.py` | citation details via the SQL Statement API with **named parameters**; page images via the Files API, **only** under `PAGE_IMAGE_ROOT`; LRU caches; `annotate_page` (Pillow) |
| `store/` | `ConversationStore` protocol; `SQLiteStore` (local/tests) and `PostgresStore` (Lakebase, OAuth-token connection pool) |
| `static/` | chat UI and citation viewer (vanilla JS; all text inserted with `textContent`) |

## 3. Request flow (`POST /api/chat`)
```
1  user = X-Forwarded-Email (or LOCAL_USER_ID locally)
2  session = store.get_session(session_id, user)         # 404 if missing or another user's
3  if client_message_id seen -> return stored answer      # idempotent retry
4  history = store.recent_turns(session, user, 5)         # last 5 Q/A pairs, oldest first
5  query = condense(message, history)                     # Haiku; skipped on the first turn
6  hits = diversify(vector_search(query, 3 x 8), limit 8, max 4 per doc)
7  answer = Sonnet([system] + history + [passages + question])
8  citations = [n] in answer -> hits[n-1] (+ boxes / span already on the hit)
9  store.append_turn(...)                                 # one transaction, row-locked
10 background: session title (first turn only)
```

## 4. Conversation history (last 5 turns)
- **Stored:** every turn is two rows (`user`, `assistant`) with the same `turn_index`. The assistant row also stores the citations (with boxes), the rewritten query, the request id and the latency.
- **Window:** `recent_turns` returns messages with `turn_index > turn_count − HISTORY_TURNS`, so always the latest 5 complete pairs (verified: turn 7 sends turns 2–6).
- **Used twice:** (a) to rewrite the follow-up into a standalone search query; (b) as prior chat messages for the answer model, so pronouns and follow-ups resolve.
- **Facts still come from retrieval:** the system prompt says history is for understanding the question, not a source of company facts.
- **Bounded cost:** 5 turns of history plus 8 passages of about 80 tokens each keeps prompts small.

## 5. Bounding-box citations
- **Paged documents (PDF/Office/images):** the ETL stored, for each chunk, boxes `(page, x0, y0, x1, y1)` normalized to 0..1, measured on page images rendered by `ai_parse_document`. The UI overlays `div`s positioned with **percentages** on the page `<img>`, so boxes stay aligned at any display size. `annotated.png` draws the same boxes server-side for API clients.
- **Text documents (TXT/MD/HTML):** exact `[char_start, char_end)` span into the stored `source_text`. The UI draws **one box around the whole span** (the text equivalent of a page box) and scrolls to it.
- Boxes travel **with the retrieval hit**, so citations in the chat response need no extra lookup. The viewer only fetches page images and text when a user opens a citation.

## 6. Security
| Concern | Control |
|---|---|
| Who is the user | Only the platform header `X-Forwarded-Email` (Databricks Apps SSO); 401 otherwise |
| Session isolation | Every store query is scoped by `user_id`; another user's session behaves as 404 |
| SQL injection | User input reaches SQL only as Statement API **named parameters**; configured table names are validated as `catalog.schema.name` |
| Arbitrary file read | Page images are served only if the stored path starts with `PAGE_IMAGE_ROOT` (`/Volumes/...`) |
| XSS | The UI never uses `innerHTML` for model or document text |
| Input limits | message ≤ 4000 chars, ids validated by pattern, feedback comment ≤ 2000 |
| Secrets | `.env` is git-ignored (mode 600); in Databricks Apps the service principal authenticates with no secrets in code |
| Headers | `x-content-type-options: nosniff`, `x-frame-options: SAMEORIGIN`, `referrer-policy: same-origin` |
| Prompt injection | Passages are declared to be data in the system prompt; answers must cite passages |

## 7. Configuration
All settings are environment variables; see [.env.example](../.env.example). The defaults are tuned by evaluation (`NUM_RESULTS=8`, `MAX_CHUNKS_PER_DOC=4`, `HISTORY_TURNS=5`).

## 8. Deployment (next step)
`app.yaml` is ready for Databricks Apps. Before production: switch `STORE_BACKEND=postgres` (a Lakebase instance is needed; SQLite in an app container does not survive restarts), add a Databricks Asset Bundle with the app's resources (serving endpoints, SQL warehouse, index SELECT, volume READ, Lakebase), and add a GitHub Actions deploy workflow authenticated with a service principal (OIDC workload identity federation, no long-lived PAT).
