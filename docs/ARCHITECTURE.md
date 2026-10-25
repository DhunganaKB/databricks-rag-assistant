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
| `tracing.py` | MLflow Tracing: on when `MLFLOW_EXPERIMENT_ID` is set; one trace per question (spans per step; each model call a CHAT_MODEL span with tokens, created in `llm.py`), tags user/session/request id; attaches thumbs up/down to the answer's trace (`messages.trace_id`) |
| `store/` | `ConversationStore` protocol; `PostgresStore` (Lakebase, OAuth-token pool, versioned migrations) and `SQLiteStore` (in-memory test double) |
| `static/` | chat UI and citation viewer (vanilla JS; all text inserted with `textContent`) |

## 3. Request flow (`POST /api/chat`)
```
1  user = X-Forwarded-Email (or LOCAL_USER_ID locally)
2  session = store.get_session(session_id, user)         # 404 if missing or another user's
3  if client_message_id seen -> return stored answer      # idempotent retry
4  summary, history = running_summary, last 5 Q/A pairs  # short-term memory (Lakebase)
5  memories = user_memories(user, 10)                     # long-term memory (Lakebase)
6  query = condense(message, summary, history)            # Haiku; skipped on the first turn
7  hits = diversify(vector_search(query, 3 x 8), limit 8, max 4 per doc)
8  answer = Sonnet([system + memories + summary] + history + [passages + question])
9  citations = [n] in answer -> hits[n-1] (+ boxes / span already on the hit)
10 store.append_turn(...)                                 # one transaction, row-locked
11 background: title (turn 1) · fold evicted turns into the summary · extract memories every 3 turns
```

## 4. Memory: all of it stored in Databricks (Lakebase)
Nothing is persisted on the machine running the app. Conversations and memory live in the **Lakebase** instance `rag-memory-dev`, schema `rag_memory` (registered in Unity Catalog as catalog `rag_memory_dev`). Tables are created by versioned migrations (`src/rag_app/store/migrations/*.sql`, applied by `make migrate` or automatically at startup).

| Table | Memory type | Contents |
|---|---|---|
| `sessions` | short-term | one row per conversation: owner, title, `turn_count`, **`running_summary`** + `summary_upto_turn` |
| `messages` | short-term | every question and answer (with citations, boxes, rewritten query, latency) |
| `user_memories` | **long-term** | durable facts about a user (profile/preference), de-duplicated, user-visible, hard-deletable |
| `feedback` | quality loop | thumbs up/down + comment per answer |
| `schema_migrations` | operations | applied migration versions |

**Short-term memory (one conversation)**
- The **last 5 question/answer pairs** (`HISTORY_TURNS`) go to the model verbatim, as prior chat messages.
- Every turn that leaves that window is folded into `sessions.running_summary` in the background (helper model, at most 150 words). From turn 7 on, the prompt holds *summary of turns 1…n−5* plus *the last 5 turns verbatim*, so long chats keep their context while the prompt stays bounded.
- Both are used to rewrite a follow-up into a standalone search query.

**Long-term memory (across conversations)**
- Every `MEMORY_EVERY_N_TURNS` (3) turns, the helper model extracts facts the user stated about **themselves** (office, role, tenure, employee/contractor, answer-format preferences) into `user_memories`.
- Sensitive data (health, religion, credentials, IDs, phone numbers, email addresses, salary…) is excluded twice: by the extraction prompt and by a regex filter in `agent.py`.
- Duplicates are rejected by a unique index on `(user_id, normalized)`.
- Each turn injects up to 10 recent memories as "Known facts about the user (may be outdated)"; company documents always take precedence. `last_used_at` is updated when a memory is used.
- Users can see and delete them (UI: *What do you remember about me?*; API: `/api/memories`). Deletes are **hard deletes**.

**Background work never affects the answer:** title, summary and memory extraction run after the response is sent, each step isolated (one failing step does not block the others). Model output cut off at `max_tokens` (`finish_reason=length`, which happens when the model spends tokens on reasoning) is **rejected rather than stored**.

**Connection and auth:** a psycopg connection pool where every new connection fetches a short-lived Databricks credential (`generate_database_credential`) as its password; connections are recycled before it expires. Locally you connect as your Databricks user; in Databricks Apps as the app's service principal.

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
| Personal data | Memories exclude sensitive categories, are visible to the user, and are hard-deleted on request; all data stays in Databricks |
| Headers | `x-content-type-options: nosniff`, `x-frame-options: SAMEORIGIN`, `referrer-policy: same-origin` |
| Prompt injection | Passages are declared to be data in the system prompt; answers must cite passages |

## 7. Configuration
All settings are environment variables; see [.env.example](../.env.example). The defaults are tuned by evaluation (`NUM_RESULTS=8`, `MAX_CHUNKS_PER_DOC=4`, `HISTORY_TURNS=5`).

## 8. Deployment
Deployed as the Databricks App `rag-assistant-dev` by GitHub Actions on every push to `main`, signed in as the deployment service principal `sp-rag-deployer`. The app runs as its own, automatically created service principal with the access declared in `resources/app.yml`. Step-by-step setup: [DEPLOYMENT.md](DEPLOYMENT.md).
