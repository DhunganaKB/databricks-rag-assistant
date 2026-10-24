"""Create or upgrade the Lakebase (Postgres) tables for conversation history and memory.

Applies every pending SQL file in src/rag_app/store/migrations/ in order (idempotent) and
prints the resulting tables with row counts.

    uv run --env-file .env python scripts/migrate.py                 # schema from PG_SCHEMA (rag_memory)
    uv run --env-file .env python scripts/migrate.py --schema rag_memory_test --drop   # throwaway schema
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rag_app.store.postgres import PostgresStore, make_pool  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--instance", default=os.getenv("LAKEBASE_INSTANCE"))
    p.add_argument("--schema", default=os.getenv("PG_SCHEMA", "rag_memory"))
    p.add_argument("--drop", action="store_true", help="DROP the schema first (only for throwaway test schemas)")
    args = p.parse_args()
    if not args.instance:
        sys.exit("set LAKEBASE_INSTANCE in .env or pass --instance")

    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    pool = make_pool(w, args.instance, max_size=2)
    store = PostgresStore(pool, args.schema)
    if args.drop:
        if not args.schema.endswith("_test"):
            sys.exit("--drop is only allowed for schemas ending in _test")
        with pool.connection() as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {args.schema} CASCADE")
        print(f"dropped schema {args.schema}")

    applied = store.ensure_schema()
    print(f"instance={args.instance} schema={args.schema} applied now: {applied or 'nothing (up to date)'}")
    with pool.connection() as conn:
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s ORDER BY 1", (args.schema,)
        ).fetchall()
        for (table,) in tables:
            count = conn.execute(f"SELECT count(*) FROM {args.schema}.{table}").fetchone()[0]
            print(f"  {args.schema}.{table:18s} {count} rows")
    pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
