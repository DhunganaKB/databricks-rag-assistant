# RAG Knowledge Assistant (Azure Databricks)

A retrieval-augmented chat application over the RAG-ready data produced by the sibling **ETL** project. It runs locally today; deployment to Databricks Apps via GitHub Actions comes next.

- **Grounded answers with citations.** Every fact is cited as `[n]`.
- **Bounding boxes in the source document.** Click a citation to see the cited passage boxed on the rendered PDF page, or highlighted in the text document. The same boxes are available as JSON and as an annotated PNG.
- **Conversation history.** The last **5** question/answer pairs go to the model (`HISTORY_TURNS`), and follow-ups such as "And in New York?" are rewritten into standalone search queries.
- **Production practices.** Sessions are scoped per user, retries are idempotent, SQL is parameterized, the image path is guarded, requests carry IDs, and dependencies are locked. There are 34 tests (90% coverage), lint and format checks, CI, an evaluation harness, and a smoke test.

```
Browser ─▶ FastAPI app ─┬─▶ Vector Search  gold.chunks_index      (hybrid; hits carry boxes/spans)
  chat UI +             ├─▶ Claude Sonnet   answers  ·  Claude Haiku  query rewrite + titles
  source viewer         ├─▶ SQL warehouse   gold.citation_locations, gold.rag_documents (viewer)
                        ├─▶ Files API       page images (silver.page_images volume)
                        └─▶ History store   SQLite (local)  |  Lakebase Postgres (Databricks)
```

## Quick start (local)

Prerequisites: [uv](https://docs.astral.sh/uv/), and access to the Azure Databricks workspace with the ETL outputs.

```bash
make install
```
```bash
scripts/bootstrap_env.sh
```
```bash
make run
```
Open http://localhost:8000 and click a suggestion. In a second terminal:
```bash
make smoke
```

| Command | What it does |
|---|---|
| `make install` | `uv sync`: creates `.venv` from `uv.lock` |
| `make lint` / `make format` | ruff lint + format check / auto-fix |
| `make test` | 34 unit + API tests, no workspace needed |
| `make run` | local server with `.env` on port 8000 |
| `make smoke` | 16 end-to-end checks against the running app |
| `make eval` | 25-question evaluation against the real index and models |
| `make requirements` | regenerate `requirements.txt` (for Databricks Apps) from `uv.lock` |

## Documentation
- [docs/BUILD_LOG.md](docs/BUILD_LOG.md): the full step-by-step record of how this repo was built and verified (commands, decisions, results, issues and fixes).
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, request flow, history, citation/box model, security.
- [docs/API.md](docs/API.md): HTTP API reference.

## Repository layout
```
src/rag_app/          application package
  api.py              FastAPI app factory, routes, middleware, identity
  agent.py            one RAG turn: history -> rewrite -> retrieve -> answer -> cite -> persist
  retriever.py        Vector Search hybrid retrieval; parses boxes/spans from each hit
  documents.py        citation viewer data (SQL warehouse + Files API), annotated PNG
  llm.py              Foundation Model endpoints via the OpenAI-compatible API
  prompts.py          versioned prompts
  config.py models.py settings and API models
  store/              conversation history: sqlite.py (local), postgres.py (Lakebase)
static/               chat UI + citation viewer (vanilla JS, no build step)
tests/                unit + API tests with fakes (no network)
scripts/              bootstrap_env.sh, smoke_test.py, run_eval.py
eval/eval_set.json    evaluation questions with expected facts
app.yaml              Databricks Apps runtime config (deployment step)
.github/workflows/    CI (lint, format, tests, lockfile check)
```
