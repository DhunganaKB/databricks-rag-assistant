# RAG Knowledge Assistant (Azure Databricks)

A retrieval-augmented chat application over the RAG-ready data produced by the sibling **ETL** project. It runs locally and deploys to Databricks Apps (`rag-assistant-dev`) through GitHub Actions on every push to `main`.

- **Grounded answers with citations.** Every fact is cited as `[n]`.
- **Bounding boxes in the source document.** Click a citation to see the cited passage boxed on the rendered PDF page, or highlighted in the text document. The same boxes are available as JSON and as an annotated PNG.
- **Short-term memory.** The last **5** question/answer pairs go to the model verbatim; older turns are folded into a running summary. Follow-ups such as "And in New York?" are rewritten into standalone search queries.
- **Long-term memory.** Facts users state about themselves (office, contract type, preferences) are remembered across conversations, are visible to the user, and can be deleted.
- **Nothing stored locally.** Sessions, messages, summaries, memories and feedback live in **Databricks Lakebase** (`rag-memory-dev`, schema `rag_memory`, Unity Catalog catalog `rag_memory_dev`).
- **Production practices.** Sessions are scoped per user, retries are idempotent, SQL is parameterized, the image path is guarded, requests carry IDs, and dependencies are locked. There are 48 unit/API tests (90% coverage) plus 4 Lakebase integration tests, lint and format checks, CI, an evaluation harness, and a smoke test.

```
Browser ─▶ FastAPI app ─┬─▶ Vector Search  gold.chunks_index      (hybrid; hits carry boxes/spans)
  chat UI +             ├─▶ Claude Sonnet   answers  ·  Claude Haiku  query rewrite + titles
  source viewer         ├─▶ SQL warehouse   gold.citation_locations, gold.rag_documents (viewer)
                        ├─▶ Files API       page images (silver.page_images volume)
                        └─▶ Lakebase        rag_memory: sessions, messages, user_memories, feedback
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
make migrate
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
| `make test` | 48 unit + API tests, no workspace needed |
| `make test-integration` | 4 tests of the Lakebase store in a throwaway schema |
| `make migrate` | create/upgrade the Lakebase tables (`rag_memory`) |
| `make run` | local server with `.env` on port 8000 |
| `make smoke` | 20 end-to-end checks against the running app (cleans up its data) |
| `make eval` | 25-question evaluation against the real index and models |
| `make requirements` | regenerate `requirements.txt` (for Databricks Apps) from `uv.lock` |

## Documentation
- [docs/BUILD_LOG.md](docs/BUILD_LOG.md): the full step-by-step record of how this repo was built and verified (commands, decisions, results, issues and fixes).
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, request flow, history, citation/box model, security.
- [docs/API.md](docs/API.md): HTTP API reference.
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md): deploy to Databricks Apps via GitHub Actions, step by step (service principal, permissions, GitHub environment).

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
  store/              postgres.py (Lakebase) + migrations/*.sql; sqlite.py = in-memory test double
static/               chat UI + citation viewer (vanilla JS, no build step)
tests/                unit + API tests with fakes (no network)
scripts/              bootstrap_env.sh, migrate.py, post_deploy.py, smoke_test.py, run_eval.py
eval/eval_set.json    evaluation questions with expected facts
app.yaml              Databricks Apps runtime config (environment-agnostic, valueFrom)
databricks.yml        Asset Bundle: target dev (the current workspace) and its variables
resources/app.yml     the app and its resources (permissions)
.github/workflows/    ci.yml (tests), deploy.yml + _deploy.yml (deploy on push to main)
```
