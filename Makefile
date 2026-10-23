# Common developer commands. Everything runs through uv (locked dependencies).
.PHONY: install lint format test test-integration run smoke eval requirements check

install:            ## create .venv with app + dev dependencies from uv.lock
	uv sync

lint:               ## static checks (ruff)
	uv run ruff check src tests scripts
	uv run ruff format --check src tests scripts

format:             ## auto-format
	uv run ruff format src tests scripts
	uv run ruff check --fix src tests scripts

test:               ## unit + API tests (no workspace needed)
	uv run pytest --cov --cov-report=term-missing

test-integration:   ## live tests against the workspace (needs .env)
	RAG_INTEGRATION=1 uv run pytest -m integration

run:                ## run the app locally on http://localhost:8000 (loads .env)
	uv run --env-file .env uvicorn rag_app.api:app --app-dir src --reload --port 8000

smoke:              ## end-to-end checks against the locally running app
	uv run --env-file .env python scripts/smoke_test.py --base-url http://localhost:8000

eval:               ## offline evaluation against the real index + model
	uv run --env-file .env python scripts/run_eval.py

requirements:       ## regenerate requirements.txt (used by Databricks Apps) from uv.lock
	uv export --no-dev --no-hashes --no-emit-project --format requirements-txt -o requirements.txt

check: lint test    ## what CI runs
