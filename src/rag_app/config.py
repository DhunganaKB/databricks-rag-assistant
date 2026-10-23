"""Settings from environment variables (12-factor). The same code runs locally (values from
.env), in tests (constructed directly) and in Databricks Apps (values from app.yaml)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# Fully qualified Unity Catalog names are interpolated into SQL, so they are validated.
_UC_NAME = re.compile(r"^[A-Za-z0-9_]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")


def _int(name: str, default: int) -> int:
    return int(os.getenv(name) or default)


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
    history_turns: int = 5
    store_backend: str = "sqlite"
    sqlite_path: str = ".data/rag_app.db"
    lakebase_instance: str = ""
    pg_schema: str = "rag_app"
    num_results: int = 8
    max_chunks_per_doc: int = 4
    max_answer_tokens: int = 900
    assistant_name: str = "Knowledge Assistant"
    local_user_id: str = ""

    def __post_init__(self) -> None:
        for field_name in ("vs_index", "citations_view", "documents_view"):
            uc_name(getattr(self, field_name), field_name)
        if not self.page_image_root.startswith("/Volumes/"):
            raise ValueError("page_image_root must be a /Volumes/... path")
        if not 0 <= self.history_turns <= 20:
            raise ValueError("history_turns must be between 0 and 20")
        if self.store_backend not in ("sqlite", "postgres"):
            raise ValueError("store_backend must be sqlite or postgres")

    @classmethod
    def from_env(cls) -> Settings:
        root = os.getenv("PAGE_IMAGE_ROOT", "/Volumes/steampunk/dev_kbdhunga_rag_silver/page_images/")
        return cls(
            vs_index=os.getenv("VS_INDEX", "steampunk.dev_kbdhunga_rag_gold.chunks_index"),
            citations_view=os.getenv("CITATIONS_VIEW", "steampunk.dev_kbdhunga_rag_gold.citation_locations"),
            documents_view=os.getenv("DOCUMENTS_VIEW", "steampunk.dev_kbdhunga_rag_gold.rag_documents"),
            page_image_root=root if root.endswith("/") else root + "/",
            sql_warehouse_id=os.getenv("SQL_WAREHOUSE_ID", ""),
            llm_endpoint=os.getenv("LLM_ENDPOINT", "databricks-claude-sonnet-5-5"),
            helper_llm_endpoint=os.getenv("HELPER_LLM_ENDPOINT", "databricks-claude-haiku-5-5"),
            history_turns=_int("HISTORY_TURNS", 5),
            store_backend=os.getenv("STORE_BACKEND", "sqlite"),
            sqlite_path=os.getenv("SQLITE_PATH", ".data/rag_app.db"),
            lakebase_instance=os.getenv("LAKEBASE_INSTANCE", ""),
            pg_schema=os.getenv("PG_SCHEMA", "rag_app"),
            num_results=_int("NUM_RESULTS", 8),
            max_chunks_per_doc=_int("MAX_CHUNKS_PER_DOC", 4),
            max_answer_tokens=_int("MAX_ANSWER_TOKENS", 900),
            assistant_name=os.getenv("ASSISTANT_NAME", "Knowledge Assistant"),
            local_user_id=os.getenv("LOCAL_USER_ID", ""),
        )
