"""Post-deployment step (run by the pipeline after `bundle deploy`).

  0. Unity Catalog: grant the app's service principal USE CATALOG / USE SCHEMA and read access
     to the gold contract (index, views, page-image volume). Explicit and idempotent, so
     access never depends on implicit behavior.
  1. apply pending Lakebase schema migrations (as the identity running this script: the deployment
     service principal in CI, which therefore owns the tables in staging/prod)
  2. make sure the app's service principal has a Postgres role
  3. grant that role data access only (SELECT/INSERT/UPDATE/DELETE), no DDL: the app can
     read and write conversations and memories but cannot change the schema
  4. verify the grants

    uv run python scripts/post_deploy.py --app rag-assistant-staging --instance rag-memory-dev

One-time bootstrap per environment, run by the Lakebase instance owner (docs/DEPLOYMENT.md):
creates the environment's database and lets the deployer service principal create its schema.
    uv run --env-file .env python scripts/post_deploy.py --instance rag-memory-dev \\
        --database rag_staging --bootstrap-for <deployer-application-id>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rag_app.config import uc_name  # noqa: E402
from rag_app.store.postgres import PostgresStore, make_pool  # noqa: E402

DML = "SELECT, INSERT, UPDATE, DELETE"


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def ensure_role(conn, role: str) -> None:
    """Databricks Apps normally creates the role when the database resource is attached;
    create it explicitly if it is missing (databricks_auth extension)."""
    if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
        print(f"role {role} exists")
        return
    conn.execute("CREATE EXTENSION IF NOT EXISTS databricks_auth")
    conn.execute("SELECT databricks_create_role(%s, 'SERVICE_PRINCIPAL')", (role,))
    print(f"created role {role}")


def tables(conn, schema: str) -> list[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s AND table_type = 'BASE TABLE' "
        "AND table_name <> 'schema_migrations' ORDER BY 1",
        (schema,),
    ).fetchall()
    return [r[0] for r in rows]


def grant_app(conn, schema: str, role: str) -> None:
    r, s = quote_ident(role), quote_ident(schema)
    conn.execute(f"GRANT USAGE ON SCHEMA {s} TO {r}")
    conn.execute(f"GRANT {DML} ON ALL TABLES IN SCHEMA {s} TO {r}")
    conn.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {s} GRANT {DML} ON TABLES TO {r}")
    conn.execute(f"REVOKE CREATE ON SCHEMA {s} FROM {r}")
    print(f"granted USAGE + {DML} on schema {schema} to {role} (no DDL)")


def verify(conn, schema: str, role: str) -> bool:
    ok = True
    for t in tables(conn, schema):
        for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            if not conn.execute("SELECT has_table_privilege(%s, %s, %s)", (role, f"{schema}.{t}", priv)).fetchone()[0]:
                print(f"MISSING {priv} on {schema}.{t} for {role}")
                ok = False
    can_create = conn.execute("SELECT has_schema_privilege(%s, %s, 'CREATE')", (role, schema)).fetchone()[0]
    print(
        f"verify: DML on all tables={'ok' if ok else 'MISSING'}; app can create objects={can_create} (expected False)"
    )
    return ok and not can_create


def uc_grant_statements(principal: str, gold_schema: str, page_image_volume: str) -> list[str]:
    """GRANT statements giving the app read access to the gold contract. Names come from
    the bundle and are validated as Unity Catalog identifiers."""
    catalog, gold = uc_name(gold_schema + ".x", "gold_schema").split(".")[:2]
    v_cat, v_schema, v_name = uc_name(page_image_volume, "page_image_volume").split(".")
    if not principal.replace("-", "").replace("@", "").replace(".", "").replace("_", "").isalnum():
        raise ValueError(f"unexpected principal name {principal!r}")
    p = f"`{principal}`"
    statements = [
        f"GRANT USE CATALOG ON CATALOG `{catalog}` TO {p}",
        f"GRANT USE SCHEMA ON SCHEMA `{catalog}`.`{gold}` TO {p}",
        *(
            f"GRANT SELECT ON TABLE `{catalog}`.`{gold}`.`{t}` TO {p}"
            for t in ("chunks_index", "citation_locations", "rag_documents")
        ),
        f"GRANT USE CATALOG ON CATALOG `{v_cat}` TO {p}",
        f"GRANT USE SCHEMA ON SCHEMA `{v_cat}`.`{v_schema}` TO {p}",
        f"GRANT READ VOLUME ON VOLUME `{v_cat}`.`{v_schema}`.`{v_name}` TO {p}",
    ]
    return list(dict.fromkeys(statements))  # de-duplicate, keep order


def uc_grants(w, warehouse_id: str, principal: str, gold_schema: str, page_image_volume: str) -> None:
    """Run the grants on a SQL warehouse (the deployer needs MANAGE on these objects, see
    docs/DEPLOYMENT.md step 6)."""
    import time

    from databricks.sdk.service.sql import StatementState

    for sql in uc_grant_statements(principal, gold_schema, page_image_volume):
        resp = w.statement_execution.execute_statement(statement=sql, warehouse_id=warehouse_id, wait_timeout="50s")
        while resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
            time.sleep(2)
            resp = w.statement_execution.get_statement(resp.statement_id)
        if resp.status.state != StatementState.SUCCEEDED:
            raise RuntimeError(f"{sql}: {resp.status.error.message if resp.status.error else resp.status.state}")
    print(f"Unity Catalog: granted {principal} USE + SELECT on {gold_schema} and READ VOLUME on {page_image_volume}")


def bootstrap(w, instance: str, database: str, deployer: str) -> None:
    """Instance owner, once per environment: create the database and the deployer's role,
    and allow the deployer to connect and create schemas in it."""
    with (
        make_pool(w, instance, max_size=1, database="databricks_postgres") as admin_pool,
        admin_pool.connection() as conn,
    ):
        ensure_role(conn, deployer)
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,)).fetchone():
            print(f"database {database} exists")
        else:
            conn.execute(f"CREATE DATABASE {quote_ident(database)}")
            print(f"created database {database}")
        conn.execute(f"GRANT CONNECT, CREATE, TEMPORARY ON DATABASE {quote_ident(database)} TO {quote_ident(deployer)}")
        print(f"granted CONNECT, CREATE on database {database} to {deployer}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--app", help="Databricks App name (its service principal gets data access)")
    p.add_argument("--instance", required=True, help="Lakebase instance name")
    p.add_argument("--database", default="databricks_postgres", help="Postgres database of this environment")
    p.add_argument("--schema", default="rag_memory")
    p.add_argument("--bootstrap-for", help="one-time, as instance owner: create --database for this deployer id")
    p.add_argument("--warehouse-id", help="with --gold-schema/--page-image-volume: run Unity Catalog grants")
    p.add_argument("--gold-schema", help="catalog.schema of the gold contract")
    p.add_argument("--page-image-volume", help="catalog.schema.volume of the page images")
    args = p.parse_args()

    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    me = w.current_user.me().user_name
    if args.bootstrap_for:
        bootstrap(w, args.instance, args.database, args.bootstrap_for)
        return 0
    pool = make_pool(w, args.instance, max_size=1, database=args.database)
    try:
        applied = PostgresStore(pool, args.schema).ensure_schema()
        print(f"migrations as {me} in {args.instance}/{args.database}: {applied or 'up to date'}")
        if not args.app:
            return 0
        app = w.apps.get(args.app)
        role = app.service_principal_client_id
        if not role:
            sys.exit(f"app {args.app} has no service principal yet; run `bundle deploy` first")
        if args.warehouse_id and args.gold_schema and args.page_image_volume:
            uc_grants(w, args.warehouse_id, role, args.gold_schema, args.page_image_volume)
        with pool.connection() as conn:
            ensure_role(conn, role)
            grant_app(conn, args.schema, role)
            return 0 if verify(conn, args.schema, role) else 1
    finally:
        pool.close()


if __name__ == "__main__":
    sys.exit(main())
