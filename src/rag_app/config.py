"""Settings from environment variables (12-factor). The same code runs locally (values from
.env), in tests (constructed directly) and in Databricks Apps (values from app.yaml).

Nothing is persisted on the machine running the app: conversations and memory live in
Lakebase (Postgres) in Databricks. The `memory` backend (in-process SQLite) exists only
for unit tests."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# Fully qualified Unity Catalog names are interpolated into SQL, so they are validated.
_UC_NAME = re.compile(r"^[A-Za-z0-9_]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
_PG_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _int(name: str, default: int) -> int:
    return int(os.getenv(name) or default)


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value in (None, "") else value.strip().lower() in ("1", "true", "yes", "on")


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"environment variable {name} is required (see .env.example / app.yaml)")
    return value


_VOLUME_PATH = re.compile(r"^/Volumes/[A-Za-z0-9_]+/[A-Za-z0-9_]+/[A-Za-z0-9_]+/?$")


def volume_path(env: dict | None = None) -> str:
    """Page-image volume -> '/Volumes/<catalog>/<schema>/<volume>/'.

    PAGE_IMAGE_VOLUME accepts both forms: the path that Databricks Apps injects for a volume
    resource (`valueFrom`, e.g. /Volumes/cat/schema/vol) or the name cat.schema.vol.
    PAGE_IMAGE_ROOT (a /Volumes/... path) is the local-development alternative."""
    env = os.environ if env is None else env
    value = (env.get("PAGE_IMAGE_VOLUME") or env.get("PAGE_IMAGE_ROOT") or "").strip()
    if not value:
        raise ValueError("set PAGE_IMAGE_VOLUME (/Volumes/... path or catalog.schema.volume)")
    if value.startswith("/Volumes/"):
        if not _VOLUME_PATH.match(value):
            raise ValueError(f"page-image volume must be /Volumes/<catalog>/<schema>/<volume>, got {value!r}")
        return value.rstrip("/") + "/"
    return "/Volumes/" + uc_name(value, "PAGE_IMAGE_VOLUME").replace(".", "/") + "/"


def uc_name(value: str, what: str) -> str:
    if not _UC_NAME.match(value or ""):
        raise ValueError(f"{what} must be a catalog.schema.name identifier, got {value!r}")
    return value


@dataclass(frozen=True)
class Settings:
    vs_index: str
    citations_view: str
    documents_view: str
    page_image_root: str
    sql_warehouse_id: str
    llm_endpoint: str = "databricks-claude-sonnet-5-5"
    helper_llm_endpoint: str = "databricks-claude-haiku-5-5"
    # conversation store
    store_backend: str = "postgres"  # postgres (Lakebase) | memory (unit tests only)
    lakebase_instance: str = ""
    pg_schema: str = "rag_memory"
    auto_migrate: bool = True  # create missing tables at startup (idempotent)
    # short-term memory
    history_turns: int = 5  # last N question/answer pairs sent verbatim
    summarize_batch_turns: int = 1  # fold each turn into the running summary as it leaves the window
    # long-term memory
    long_term_memory: bool = True
    memory_every_n_turns: int = 3  # extract facts about the user every N turns
    max_memories_in_prompt: int = 10
    # retrieval / generation
    num_results: int = 8
    max_chunks_per_doc: int = 4
    max_answer_tokens: int = 2000
    assistant_name: str = "Knowledge Assistant"
    local_user_id: str = ""
    # observability: MLflow experiment that receives the traces (empty = tracing off)
    mlflow_experiment_id: str = ""

    def __post_init__(self) -> None:
        for field_name in ("vs_index", "citations_view", "documents_view"):
            uc_name(getattr(self, field_name), field_name)
        if not self.page_image_root.startswith("/Volumes/"):
            raise ValueError("page_image_root must be a /Volumes/... path")
        if not 0 <= self.history_turns <= 20:
            raise ValueError("history_turns must be between 0 and 20")
        if self.store_backend not in ("postgres", "memory"):
            raise ValueError("store_backend must be postgres (Lakebase) or memory (tests only)")
        if self.store_backend == "postgres" and not (self.lakebase_instance or os.getenv("PGHOST")):
            # Locally: LAKEBASE_INSTANCE. In Databricks Apps: the database resource sets PGHOST.
            raise ValueError("STORE_BACKEND=postgres needs LAKEBASE_INSTANCE (local) or PGHOST (Databricks Apps)")
        if not _PG_IDENT.match(self.pg_schema):
            raise ValueError("pg_schema must be a lowercase Postgres identifier")
        if self.memory_every_n_turns < 1 or self.summarize_batch_turns < 1:
            raise ValueError("memory_every_n_turns and summarize_batch_turns must be >= 1")

    @classmethod
    def from_env(cls) -> Settings:
        # Environment-specific values have no defaults: a missing value fails fast at startup.
        return cls(
            vs_index=_required("VS_INDEX"),
            citations_view=_required("CITATIONS_VIEW"),
            documents_view=_required("DOCUMENTS_VIEW"),
            page_image_root=volume_path(),
            sql_warehouse_id=_required("SQL_WAREHOUSE_ID"),
            llm_endpoint=os.getenv("LLM_ENDPOINT", "databricks-claude-sonnet-5-5"),
            helper_llm_endpoint=os.getenv("HELPER_LLM_ENDPOINT", "databricks-claude-haiku-5-5"),
            store_backend=os.getenv("STORE_BACKEND", "postgres"),
            lakebase_instance=os.getenv("LAKEBASE_INSTANCE", ""),
            pg_schema=os.getenv("PG_SCHEMA", "rag_memory"),
            auto_migrate=_bool("AUTO_MIGRATE", True),
            history_turns=_int("HISTORY_TURNS", 5),
            summarize_batch_turns=_int("SUMMARIZE_BATCH_TURNS", 1),
            long_term_memory=_bool("LONG_TERM_MEMORY", True),
            memory_every_n_turns=_int("MEMORY_EVERY_N_TURNS", 3),
            max_memories_in_prompt=_int("MAX_MEMORIES_IN_PROMPT", 10),
            num_results=_int("NUM_RESULTS", 8),
            max_chunks_per_doc=_int("MAX_CHUNKS_PER_DOC", 4),
            max_answer_tokens=_int("MAX_ANSWER_TOKENS", 2000),
            assistant_name=os.getenv("ASSISTANT_NAME", "Knowledge Assistant"),
            local_user_id=os.getenv("LOCAL_USER_ID", ""),
            mlflow_experiment_id=os.getenv("MLFLOW_EXPERIMENT_ID", "").strip(),
        )
