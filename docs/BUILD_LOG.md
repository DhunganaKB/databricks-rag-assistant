# Build Log: RAG application (local build, 2026-10-23)

A chronological record of every step taken to build and verify this repository: what was done, the exact commands, why, the result, and the problems found and how they were fixed. Follow it top to bottom to reproduce the build on a new machine.

**Inputs from earlier work**
- Workspace: `https://adb-7405616448132971.11.azuredatabricks.net` (Azure Central US, Unity Catalog, serverless).
- The **ETL** project (`../ETL`) already produced the gold layer: `steampunk.dev_kbdhunga_rag_gold.chunks_index` (84 chunks), `citation_locations` (85 regions), `rag_documents` (13 docs), and page images in `steampunk.dev_kbdhunga_rag_silver.page_images`.
- Credentials: a personal access token in `AzureDatabricks/.env` (`DAP=...`).

**Requirements for this repo**
1. A complete RAG application in its own git repo, ready for CI/CD (GitHub Actions) later.
2. The final answer must carry **bounding boxes** that point into the source document.
3. **Conversation history:** the last 5 turns go into the model's context.
4. Tested locally, best practices, and every step recorded (this file).

---

## Step 1: Probe the workspace before writing code

**Why:** confirm every dependency the app needs exists and see its real output shapes, instead of guessing.

```bash
# (run from ../ETL, which already has an authenticated helper)
uv run --no-project --python 3.11 --with databricks-sdk --with openai --with httpx python - <<'EOF'
# query the index, call both Claude endpoints, list warehouses and Lakebase instances
EOF
```

**Results**
| Check | Result |
|---|---|
| Vector Search `HYBRID` query | 1.2 s; every hit has `locations_json`, `doc_format`, `page_start`, `char_start/char_end` |
| `databricks-claude-sonnet-5-5` | answers (1.4 s) |
| `databricks-claude-haiku-5-5` | answers (2.3 s) |
| SQL warehouse | `e21be0a5a64a99a6` (Serverless Starter Warehouse) |
| Lakebase instances | **none** on this workspace |

**Decisions**
- Answer model: **Claude Sonnet 5.5**. Helper model (query rewrite, titles): **Claude Haiku 5.5** (faster and cheaper).
- No Lakebase yet, so the history store is **pluggable**: SQLite for local development and tests, Postgres/Lakebase for the deployed app (enabled in the deployment step).
- Index numbers come back as strings (`"2.0"`) and NULLs as `None`, so the parser has to be tolerant.

## Step 2: Create the repository skeleton

```bash
cd AzureDatabricks/RAG
git init -b main
mkdir -p src/rag_app/store static tests eval scripts docs .github/workflows
echo "3.11" > .python-version          # Databricks Apps runs Python 3.11
```

Files created and why:
| File | Purpose |
|---|---|
| `pyproject.toml` | Dependencies, dev group (pytest, pytest-cov, ruff), pytest/ruff/coverage config. `src/` layout so tests import the package the same way production does |
| `uv.lock` | Exact, reproducible dependency versions (`uv lock`) |
| `.gitignore` | Excludes `.env`, `.data/`, `output/`, venvs, caches, `.databricks/` |
| `.env.example` | Every setting with comments; copied to `.env` locally (never committed) |
| `Makefile` | One-word commands: `install lint format test run smoke eval requirements check` |

## Step 3: Application package (`src/rag_app`)

Written in this order, each with a single responsibility:

1. **`config.py`**: `Settings` dataclass from environment variables. Validates that `VS_INDEX`, `CITATIONS_VIEW` and `DOCUMENTS_VIEW` are `catalog.schema.name` identifiers (they are interpolated into SQL), that `PAGE_IMAGE_ROOT` is a `/Volumes/` path, and that `HISTORY_TURNS` is 0–20.
2. **`models.py`**: Pydantic models. `Box(page, x0, y0, x1, y1, estimated)` with 0..1 coordinates, origin top-left; `TextSpan(char_start, char_end, line_start, line_end)`; `Citation` carries `boxes` and `span`, so **the final answer contains the bounding boxes**.
3. **`retriever.py`**: Vector Search `HYBRID` query; `hit_from_row` parses `locations_json` into `Box`es and char/line columns into a `TextSpan`, tolerating strings and NULLs; `diversify` caps chunks per document.
4. **`llm.py`**: OpenAI-compatible client on `<host>/serving-endpoints`.
5. **`prompts.py`**: versioned prompts: answer rules (cite `[n]`, decline when not found, passages are data), standalone-query rewrite, title.
6. **`store/`**: `ConversationStore` protocol + `SQLiteStore` + `PostgresStore`. Sessions and messages, all scoped by `user_id`; `recent_turns(session, user, n)` returns the last *n* question/answer pairs; `append_turn` is one transaction; `client_message_id` gives idempotent retries.
7. **`agent.py`**: one turn: session → idempotency → **last 5 turns** → rewrite follow-up (Haiku) → retrieve → answer (Sonnet, history passed as prior messages) → map `[n]` to citations **with boxes/spans** → persist.
8. **`documents.py`**: viewer data from the gold views via the SQL Statement API with **named parameters**; page images via the Files API, only under `PAGE_IMAGE_ROOT`; LRU caches; `annotate_page` draws boxes into a PNG.
9. **`api.py`**: FastAPI app factory `create_app(settings, agent, documents)` (tests inject fakes), request-id and security-header middleware, identity from `X-Forwarded-Email` (Databricks Apps SSO) or `LOCAL_USER_ID` locally.

## Step 4: User interface (`static/`)

- Three panes: conversations, chat, **source viewer**.
- `[n]` markers in the answer become clickable chips, and a source list sits under each answer.
- Clicking a citation opens the viewer:
  - **PDF:** the page image with `div` boxes positioned by **percent** (`left = x0·100%` …), so they stay aligned at any size. A link downloads the server-drawn annotated PNG.
  - **Text:** the document with **one box drawn around the cited span**, scrolled into view.
- The first citation opens automatically after each answer.
- Each answer shows "history: N turn(s)" and the rewritten search query.
- Model and document text are inserted with `textContent` only (XSS-safe). Dark mode and a mobile layout are included.

## Step 5: Tests

```bash
uv lock && uv sync
uv run pytest
```
- `tests/conftest.py`: deterministic fakes for the retriever, LLM and document service, plus in-memory SQLite. No network.
- `test_agent.py`: **history window = exactly the last 5 turns** (turn 7 sends turns 2–6), configurable window, follow-up rewrite, citations carry boxes/spans, session isolation, idempotency, no-answer.
- `test_api.py`: identity required, citations with boxes in the JSON, session privacy across users, input validation, citation detail and page image, **annotated PNG pixels actually drawn**, feedback.
- `test_retriever_and_config.py`: parsing of real Vector Search rows, tolerant box parsing, identifier validation.
- `test_llm.py`: temperature-rejection retry (see Issue 3).
- `test_documents.py`: SQL uses bound parameters (the id never appears in the SQL text), page grouping, caching, **path guard blocks images outside the volume**.

**Issue 1: build failed: `README.md` missing.** `pyproject.toml` references it. Fix: a placeholder README, replaced at the end.

**Result:** 26 → 28 → **34 tests, all passing**.

## Step 6: Lint and format

```bash
uv run ruff check src tests scripts --fix
uv run ruff format src tests scripts
```
**Issue 2:** 15 findings (mostly import order), plus E501 on prompt prose and SQL strings. Fix: auto-fix; let `ruff format` own line length (`E501` ignored, with the reason documented in `pyproject.toml`); tell isort that `conftest` is a local module. **Result:** `All checks passed!`

## Step 7: Run locally against the real workspace

```bash
scripts/bootstrap_env.sh       # creates .env from .env.example with the PAT from ../.env (chmod 600, git-ignored)
make run                       # = uv run --env-file .env uvicorn rag_app.api:app --app-dir src --port 8000
curl -s localhost:8000/api/health
```
**Issue A: the bootstrap script printed the token.** The echo used `${token:-missing}`, which expands to the token itself. Caught in review (the output had been redacted); fixed to print only "token found/missing".

**Issue 3: every chat returned 500.** The server log showed a 400 from the serving endpoint: *"Model us.anthropic.claude-sonnet-5-5 does not support the temperature parameter."* Fix: `DatabricksLLM` sends `temperature` when it can; on that specific 400 it retries without it and **remembers** the endpoint, so later calls skip the failing attempt. Verified live: both Claude endpoints were added to the no-temperature set after one retry each. Unit-tested in `test_llm.py`.

**Issue 4: dependency bloat.** The SDK's `get_open_ai_client()` is deprecated in favor of `databricks-openai`, but that package pulled in the full MLflow stack (scikit-learn, pandas, pyarrow, Flask, SQLAlchemy …): **140 packages, 623 MB**. That's too heavy for a web app (slow deployments, larger attack surface). Fix: a 15-line equivalent, a plain `OpenAI` client with an `httpx` auth hook that adds fresh Databricks credentials per request (works with a PAT locally and an OAuth service principal in Databricks Apps). **Result: 40 packages, 114 MB.**

## Step 8: End-to-end smoke test

```bash
make smoke      # = uv run --env-file .env python scripts/smoke_test.py --base-url http://localhost:8000
```
It runs a 7-turn conversation, then checks citations, the viewer endpoints, idempotency, privacy and the no-answer case.

**Issue 5: one check failed.** "span points at the cited text" compared the chunk's first line `### 1.1 Annual allowance` with the source. That inline markdown heading is added by the chunker, so it isn't verbatim in the source. The app was right; the check was wrong. Fix: compare a body line. **Result: 16/16 passed.**

Key evidence from the run:
```
history used per turn: [0, 1, 2, 3, 4, 5, 5]                  <- grows, then capped at 5
Q2 "And how did it compare with Q3 2025?"  -> searched "Revenue in Q3 2026 compared with Q3 2025"
Q7 "And in New York?"                       -> searched "What is the hotel limit in New York?"
PDF citation box: {'page': 1, 'x0': 0.12999, 'y0': 0.29585, 'x1': 0.64087, 'y1': 0.4472}
TXT citation span: {'char_start': 421, 'char_end': 1285, 'line_start': 11, 'line_end': 29}
```
`output/smoke_annotated.png` shows the box drawn exactly around the cited table and paragraph on page 1 of the Quarterly Business Review.

## Step 9: Check the UI in a browser

`http://localhost:8000`, at both desktop width and narrow (mobile) width.
- PDF citation: the page image with boxes exactly on the "Financial results" table and the following paragraph.
- Text citation: the PTO policy with the Vacation section highlighted (lines 11–29).
- Reopening a saved conversation from the sidebar restores all turns and their citations.

**Issue 6: two UI defects.** (a) The text highlight was outlined line by line and looked noisy; changed to **one box around the whole span**. (b) Labels repeated the document title ("QBR › QBR > 2. Financial results"); added `sectionOf()` in the UI and `Hit.section` for model context labels.

## Step 10: Evaluation

```bash
make eval       # = uv run --env-file .env python scripts/run_eval.py
```
It runs 25 questions (facts, tables, PDFs, multi-hop, multi-turn, how-to, two no-answer), scored by an LLM judge (Haiku) and code checks. The report is written to `output/eval_report.json`.

| Run | correct | retrieval_hit | cited | located | declined |
|---|---|---|---|---|---|
| 1 | 0.56 | 1.0* | 0.96 | 1.0 | 1.0 |
| 2 (after fixes) | **1.0** | **1.0** | **1.0** | **1.0** | **1.0** |

**Issue 7: judge parsing.** Most run-1 "failures" were the judge's JSON cut off at 150 tokens or returned in another shape. Fix: 400 tokens and a regex fallback for `"correct": true|false`.

**Issue 8: a real retrieval miss (`inc-1`, SEV2 response time).** The severity table was ranked 4th, but the per-document cap of 3 dropped it. *The `retrieval_hit` metric missed this because it measured raw candidates, not the chunks given to the model. Fixes:
- `MAX_CHUNKS_PER_DOC` 3 → 4 and `NUM_RESULTS` 6 → 8 (chunks average about 83 tokens, so the cost is small)
- `retrieval_hit` now measures what the model actually saw.

## Step 11: Coverage gate

```bash
uv run pytest --cov --cov-fail-under=80
```
**Issue 9:** total coverage was 71%. `documents.py` (31%) had no tests, a real gap since it holds the SQL and path-guard logic. Fix: `test_documents.py` against a fake workspace client. `store/postgres.py` needs a real Postgres server, so it is excluded from *unit* coverage (documented in `pyproject.toml`) and will be covered by integration tests in the deployment pipeline. **Result: 89.5%** (`documents.py` 94%, `agent.py` 100%).

## Step 12: CI and deployment-ready files

- `.github/workflows/ci.yml` runs on every push and PR, with no secrets: `uv sync --locked` → ruff check → ruff format --check → pytest with an **80% coverage gate** → check that `requirements.txt` matches `uv.lock`.
- `requirements.txt`: generated from `uv.lock` (`make requirements`). Databricks Apps installs from it.
- `app.yaml`: Databricks Apps command (`uvicorn rag_app.api:app --app-dir src`) and env. Endpoint names and the warehouse come from app resources (`valueFrom`). `STORE_BACKEND=postgres` is prepared but commented out until Lakebase exists.

## Step 13: Commit

```bash
git add -A
git status            # verify: no .env, no .data/, no output/
git commit -m "..."
```

---

# Part 2: Memory moved to Databricks (2026-10-24)

**Requirement:** nothing stored locally, even when running locally. Short-term and long-term memory must live in Databricks.

## Step 14: Create the Databricks storage (done by you)

```bash
scripts/dbx.sh database create-database-instance rag-memory-dev --capacity CU_1      # Lakebase (managed Postgres)
scripts/dbx.sh database create-database-catalog rag_memory_dev rag-memory-dev databricks_postgres   # register in Unity Catalog
```
Verified: instance `rag-memory-dev` is **AVAILABLE** (CU_1, host `ep-dawn-sound-e88ir4y5.database.centralus.azuredatabricks.net`), and Unity Catalog shows catalog `rag_memory_dev` (type `MANAGED_ONLINE_CATALOG`).

**Why Lakebase, not Delta tables:** every chat turn does several small reads and transactional writes. Postgres does this in milliseconds with row locks; Delta through a SQL warehouse would add 0.5–3 s per call and create many small files. Lakebase is still Databricks-managed, governed in Unity Catalog, and can be synced to Delta for analytics.

## Step 15: Code changes

| Area | Change |
|---|---|
| `config.py` | `STORE_BACKEND` is now `postgres` (default) or `memory` (tests only). The file-based SQLite option is **removed**. `LAKEBASE_INSTANCE` is required; new settings `LONG_TERM_MEMORY`, `MEMORY_EVERY_N_TURNS`, `MAX_MEMORIES_IN_PROMPT`, `SUMMARIZE_BATCH_TURNS`, `AUTO_MIGRATE` |
| `store/migrations/001_initial.sql` | Versioned DDL: `sessions` (with `running_summary`), `messages`, `user_memories`, `feedback`. Applied in order and recorded in `schema_migrations` |
| `store/postgres.py` | Migration runner; summary updates that only move forward; memories de-duplicated with `ON CONFLICT`; `touch_memories`; hard delete |
| `store/sqlite.py` | In-memory only (its constructor no longer accepts a path), so it can never write a file |
| `agent.py` | Short-term memory = running summary + last 5 turns. Long-term memory = user facts in the system prompt. Background `maintain()`: title, summary roll-up, memory extraction (each step isolated) |
| `prompts.py` | Summary and memory-extraction prompts; rule that documents outrank remembered facts |
| `api.py` | Background `maintain` after every answer; `GET/DELETE /api/memories`; health shows store and schema |
| UI | "What do you remember about me?" dialog with Forget / Forget everything; answers show summary/memory usage |
| `scripts/migrate.py` | `make migrate`: apply migrations and print tables with row counts |
| `.env`, `.env.example`, `app.yaml` | `STORE_BACKEND=postgres`, `LAKEBASE_INSTANCE=rag-memory-dev`, `PG_SCHEMA=rag_memory` |

Then deleted the old local file: `rm -rf .data`.

## Step 16: Create the tables in Lakebase

```bash
make migrate
```
```
instance=rag-memory-dev schema=rag_memory applied now: ['001_initial']
  rag_memory.feedback / messages / schema_migrations / sessions / user_memories
```
Running it again prints `nothing (up to date)` (idempotent).

## Step 17: Tests

- Unit tests (`make test`): **42 passed**, 90% coverage. New tests cover the rolling summary (turns 1–2 reach the model only through the summary), long-term memory (extraction, password filtered out, used in a *new* session, invisible to other users), de-duplication, forget, disabling memory, and maintenance steps failing independently.
- **Issue 10: two assertions passed for the wrong reason.** They checked for "Known facts about the user", which is always present in the system prompt rules. Fixed to check for the actual memory block and fact text.
- Integration tests against Lakebase (`make test-integration`): **4 passed** in a throwaway schema `rag_memory_it_test` that is created and dropped by the test. They cover turns and isolation, JSONB citations with boxes, summary only moving forward, `ON CONFLICT` de-duplication, and cascade delete.

## Step 18: Run locally against Lakebase, smoke test

```bash
make run          # log: "connecting to Lakebase instance=rag-memory-dev ... user=kbdhunga@mtu.edu"
make smoke
```
The smoke test now uses a fresh test user per run, checks summary and memory, and **deletes only the sessions it created**. When pointed at a deployed app with a real user's token, it never wipes that user's memories.

**Issue 11: the rolling summary was sometimes empty or cut off** (first run 19/20: "turn 8 uses summary" failed). Investigation:
- Background maintenance had run (`summarized_upto` 1 → 2 before turn 8), but a replay showed a stored summary of only 124 characters, cut mid-word.
- Calling the helper model directly showed that `databricks-claude-haiku-5-5` sometimes returns a **reasoning part before the text**, and **reasoning tokens count against `max_tokens`**. With a 300-token budget, 3 of 4 calls ended with `finish_reason=length`.

Fixes:
- larger budgets: summary 1500, extraction 1200, rewrite 400, title 200, answers 2000
- `DatabricksLLM.complete(require_complete=True)` raises `TruncatedOutput` instead of returning cut-off text, so background steps never store a truncated summary or memory list
- answers that hit the limit are logged
- unit tests for both cases

**Issue 12: "forget" kept the data.** After the smoke test, `user_memories` still had 4 rows: deletes were soft (`status='deleted'`). Changed to a **hard DELETE** in both stores (personal data must really be removed) and purged the 4 leftover rows.

**Issue 13: the eval script crashed silently.** It still called `SQLiteStore(":memory:")`. Fixed. Lesson: scripts are not covered by unit tests; a type checker in CI would catch signature changes like this (candidate for the deployment step).

**Final results**
| Check | Result |
|---|---|
| `make test` | 42 passed, 90% coverage |
| `make test-integration` (Lakebase) | 4 passed |
| `make smoke` | **20/20**: history 0→5 capped, summary used at turn 8, memories extracted and used in a new session ("contractors are not eligible…"), boxes and spans, idempotency, privacy, no-answer |
| `make eval` | **25/25** correct, retrieval hit 100%, every citation located |
| Lakebase after the tests | `sessions` 0, `messages` 0, `user_memories` 0 (tests clean up after themselves) |

---

## How to reproduce from scratch
```bash
git clone <repo> && cd RAG
make install
scripts/bootstrap_env.sh        # or: cp .env.example .env and set DATABRICKS_TOKEN
make check                      # lint + tests (no workspace needed)
make migrate                    # create the Lakebase tables (rag_memory)
make test-integration           # 4 Lakebase store tests
make run                        # terminal 1
make smoke                      # terminal 2: 20/20 expected
make eval                       # 25/25 expected on the sample corpus
```

## Open items for the deployment step
1. ~~Create a Lakebase instance; switch `STORE_BACKEND=postgres`; add Postgres integration tests.~~ Done (Part 2). Remaining: ownership/grants on `rag_memory` for the app's service principal.
2. Add a Databricks Asset Bundle (`databricks.yml` + `resources/app.yml`) declaring the app's resources: serving endpoints (CAN_QUERY), SQL warehouse (CAN_USE), index (SELECT), page-image volume (READ VOLUME), gold views (SELECT), Lakebase (CAN_CONNECT_AND_CREATE).
3. Add a GitHub Actions deploy workflow: service principal auth via OIDC (no long-lived token), `bundle validate` → `bundle deploy` → `bundle run` → smoke test against the deployed URL → eval gate.
4. Promote dev → prod with separate targets and catalogs.
