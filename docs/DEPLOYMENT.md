# Enterprise deployment: GitHub → Databricks Apps (Azure)

This guide deploys the RAG assistant from GitHub (`DhunganaKB/databricks-rag-assistant`) to Databricks Apps, the standard way:

- **Infrastructure as code:** a Databricks Asset Bundle (`databricks.yml`, `resources/app.yml`) declares the app, its permissions and everything it may access.
- **No secrets anywhere:** GitHub authenticates to Databricks with **OIDC workload identity federation**. The app authenticates as its own service principal.
- **Separate environments:** `staging` deploys automatically on every merge to `main`; `prod` deploys from a GitHub release after manual approval.
- **Least privilege:** one deployment service principal per environment. The app gets read-only data access, and data-only (no DDL) access to its memory tables.
- **Quality gates:** lint and tests before deploying, then a smoke test and evaluation against the *deployed* app before the job counts as successful.

Part A is the one-time setup (done once per environment). Part B is the first deployments. Part C explains where every setting lives. Part D covers day-2 operations.

Values used throughout (from your workspace):

| Name | Value |
|---|---|
| Workspace URL | `https://adb-7405616448132971.11.azuredatabricks.net` |
| Databricks account ID | `81bedc3d-4870-42c7-aa36-dd2118c81464` |
| Account console | `https://accounts.azuredatabricks.net` |
| GitHub repo | `DhunganaKB/databricks-rag-assistant` |
| OIDC token audience | `https://adb-7405616448132971.11.azuredatabricks.net/oidc/v1/token` |

---

## Overview

```
 GitHub                                                   Azure Databricks workspace
 ──────                                                   ──────────────────────────
 PR ──▶ ci (lint, tests)
 merge to main ──▶ deploy.yml ──▶ ci ──▶ _deploy(staging) ──OIDC──▶ sp-rag-deployer-staging
                                                              │   bundle deploy  → app "rag-assistant-staging"
                                                              │   post_deploy    → UC grants, Lakebase migrations + grants
                                                              │   bundle run     → app started
                                                              │   smoke + eval   → against the deployed URL
 release v1.x ──▶ deploy.yml ──▶ ci ──▶ [approval] ──▶ _deploy(prod) ──OIDC──▶ sp-rag-deployer-prod → app "rag-assistant"

 App at runtime: runs as its OWN service principal (created by Databricks Apps)
   reads:  gold.chunks_index, gold.citation_locations, gold.rag_documents, silver.page_images (UC)
   calls:  Claude Sonnet / Haiku serving endpoints, SQL warehouse
   writes: Lakebase <instance>/<database>.rag_memory (sessions, messages, user_memories, feedback)
```

| | dev (personal) | staging | prod |
|---|---|---|---|
| Deployed by | you, from your laptop (`make deploy-dev`) | GitHub Actions on merge to `main` | GitHub Actions on a published release, after approval |
| Identity | your user | `sp-rag-deployer-staging` | `sp-rag-deployer-prod` |
| App name | `rag-assistant-dev` | `rag-assistant-staging` | `rag-assistant` |
| Gold data | `steampunk.dev_kbdhunga_rag_gold` | `steampunk.dev_kbdhunga_rag_gold` (until the ETL has a staging target) | `steampunk.rag_gold` (ETL prod target) |
| Lakebase | `rag-memory-dev` / `databricks_postgres` | `rag-memory-dev` / `rag_staging` | `rag-memory-prod` / `rag_prod` |
| Bundle folder | `/Workspace/Users/<you>/.bundle/rag-assistant/dev` | `/Workspace/Users/<staging-sp>/.bundle/...` | `/Workspace/Users/<prod-sp>/.bundle/...` |

---

# Part A: One-time setup

Do A1–A8 for **staging** first. Repeat them for **prod** when you are ready (the differences are called out).

## A1. Prerequisites
- Databricks CLI ≥ 0.295 on your laptop. You need **workspace admin**, and for A4 **account admin** (or someone who has it).
- The ETL gold layer exists for the environment. Staging uses the dev gold layer, which already exists. **For prod**, deploy the ETL project's prod target first:
  ```bash
  cd ../ETL && scripts/dbx.sh bundle deploy -t prod
  ```
  ```bash
  cd ../ETL && scripts/dbx.sh bundle run rag_etl_pipeline -t prod
  ```
  This creates `steampunk.rag_gold` and `steampunk.rag_silver.page_images` (no `dev_` prefix).

## A2. Lakebase instance (memory store)
- Staging reuses `rag-memory-dev`, which already exists.
- **Prod** gets its own instance, so production conversations are fully isolated:
  ```bash
  databricks database create-database-instance rag-memory-prod --capacity CU_1
  ```
  For production, consider `--node-count 2 --enable-readable-secondaries` (high availability) and a longer `--retention-window-in-days` (point-in-time restore).

## A3. Create the deployment service principal
Workspace → **Settings → Identity and access → Service principals → Add service principal → Add new**:
- Name: `sp-rag-deployer-staging` (prod: `sp-rag-deployer-prod`). Type: *Databricks managed*.
- Open it and copy its **Application ID** (a UUID). This is `DATABRICKS_CLIENT_ID` below.
- **Configurations** tab: enable **Workspace access**. Nothing else (no admin rights).

CLI equivalent:
```bash
databricks service-principals create --json '{"displayName": "sp-rag-deployer-staging", "entitlements": [{"value": "workspace-access"}]}'
```

## A4. Let GitHub sign in as that service principal (OIDC federation, no secret)
Account console (`https://accounts.azuredatabricks.net`) → **User management → Service principals → sp-rag-deployer-staging → Credentials & secrets → Federation policies → Create policy**:

| Field | staging | prod |
|---|---|---|
| Issuer URL | `https://token.actions.githubusercontent.com` | same |
| Subject | `repo:DhunganaKB/databricks-rag-assistant:environment:staging` | `repo:DhunganaKB/databricks-rag-assistant:environment:prod` |
| Audiences | `https://adb-7405616448132971.11.azuredatabricks.net/oidc/v1/token` | same |

The **subject** binds the identity to *this repo and this GitHub environment*. A workflow in another repo, or a job that doesn't use the `staging` environment, cannot obtain the token.

CLI equivalent (account-level auth; `<SP_ID>` is the numeric ID shown in the account console):
```bash
databricks account service-principal-federation-policy create <SP_ID> --json '{"oidc_policy": {"issuer": "https://token.actions.githubusercontent.com", "subject": "repo:DhunganaKB/databricks-rag-assistant:environment:staging", "audiences": ["https://adb-7405616448132971.11.azuredatabricks.net/oidc/v1/token"]}}'
```

> **No account-admin access?** Use the fallback. In the workspace, open the service principal → **Secrets → Generate secret**, then in GitHub (A7) add the secret `DATABRICKS_CLIENT_SECRET` and the variable `DATABRICKS_AUTH_TYPE=oauth-m2m`. It works, but it's a long-lived secret you must rotate. Switch to federation when you can.

## A5. Grant the deployer what it needs (least privilege)
Run as the owner of these objects (you), for example in the SQL editor. Replace `<SP>` with the Application ID.

**Unity Catalog:** read the gold contract and pass read access on to the app's service principal (`MANAGE` lets it grant):
```sql
GRANT USE CATALOG ON CATALOG steampunk TO `<SP>`;
GRANT USE SCHEMA, SELECT, MANAGE ON SCHEMA steampunk.dev_kbdhunga_rag_gold   TO `<SP>`;  -- prod: steampunk.rag_gold
GRANT USE SCHEMA, READ VOLUME, MANAGE ON SCHEMA steampunk.dev_kbdhunga_rag_silver TO `<SP>`;  -- prod: steampunk.rag_silver
```

**Workspace objects** (UI → object → **Permissions**):

| Object | Permission for the deployer | Why |
|---|---|---|
| SQL warehouse *Serverless Starter Warehouse* | **Can manage** | run the post-deploy grants and give the app *Can use* |
| Serving endpoints `databricks-claude-sonnet-5-5`, `databricks-claude-haiku-5-5` | **Can query** (+ *Can manage* if your workspace restricts foundation models) | smoke/eval judge; grant *Can query* to the app |
| Lakebase instance (`rag-memory-dev`; prod: `rag-memory-prod`) | **Can manage** | attach the database to the app, run migrations |

## A6. Bootstrap the environment's Lakebase database
Run this from your laptop as the instance owner. It creates the database and the deployer's Postgres role, and allows the deployer to connect and create schemas. The deployer then creates (and owns) the `rag_memory` tables on its first run.
```bash
uv run --env-file .env python scripts/post_deploy.py --instance rag-memory-dev --database rag_staging --bootstrap-for <STAGING_SP_APPLICATION_ID>
```
```bash
uv run --env-file .env python scripts/post_deploy.py --instance rag-memory-prod --database rag_prod --bootstrap-for <PROD_SP_APPLICATION_ID>
```

## A7. GitHub environments, variables and protection
Repo → **Settings → Environments → New environment**:

**`staging`**
- Variables (not secrets; none of these is sensitive):
  - `DATABRICKS_HOST` = `https://adb-7405616448132971.11.azuredatabricks.net`
  - `DATABRICKS_CLIENT_ID` = staging deployer Application ID
- Deployment branches and tags: **Selected → `main`**

**`prod`**
- Variables: `DATABRICKS_HOST` (same) and `DATABRICKS_CLIENT_ID` = **prod** deployer Application ID
- **Required reviewers:** you (and a second person if available). **Prevent self-review** if you have a team.
- Deployment branches and tags: **Selected → tags `v*`**

Only for the A4 fallback: secret `DATABRICKS_CLIENT_SECRET` and variable `DATABRICKS_AUTH_TYPE=oauth-m2m` on that environment.

## A8. Protect `main`
Repo → **Settings → Rules → Rulesets → New branch ruleset** (target: `main`):
- Require a pull request before merging (1 approval if you have a team)
- Require status checks to pass: **`ci / test`**
- Block force pushes

Every change then reaches staging only through a reviewed, tested PR.

---

# Part B: Deploy

## B1. (Recommended) Rehearse with your personal dev app
This runs the same steps as the pipeline, under your identity, against the dev data:
```bash
make deploy-dev
```
This does `bundle deploy -t dev`, then `post_deploy.py` (UC grants, migrations, app grants), then `bundle run`. Then:
```bash
databricks apps get rag-assistant-dev
```
Open the URL and ask *"What was revenue in Q3 2026?"*. You should see the answer and the boxed table in the source viewer.

## B2. Deploy staging
Merge a PR to `main` (or **Actions → deploy → Run workflow → staging**). The pipeline runs:

1. `ci`: lint, format, 48 tests (80% coverage gate), lockfile check
2. `Check configuration and identity`: prints `deploying as: <staging SP>`
3. `Validate bundle` → `Deploy bundle`
4. `Unity Catalog grants, Lakebase migrations and app grants`
5. `Deploy and start the app`
6. `Smoke test (deployed app)`: 20 checks
7. `Evaluation gate (deployed app)`: 25 questions; fails below 90% correct
8. The job summary shows the app URL and the evaluation numbers. The full report is attached as an artifact.

## B3. Deploy prod
After staging is green: **Releases → Draft a new release → tag `v1.0.0` → Publish**. The `prod` job waits for an approval in **Actions**. Once approved, it runs the same steps against the prod configuration.

## B4. Give people access
The bundle grants **Can use** to the workspace group in `app_users_group` (default `users`) and **Can manage** to `app_admins_group` (default `admins`). For a restricted rollout, create a group (e.g. `rag-assistant-users`) and set it per target in `databricks.yml`:
```yaml
  prod:
    variables:
      app_users_group: rag-assistant-users
```

---

# Part C: Where every setting lives

| Kind of setting | Lives in | Example | Changed by |
|---|---|---|---|
| Per-environment infrastructure (names, data, instances) | `databricks.yml` → `targets.<env>.variables` | `app_name`, `gold_schema`, `lakebase_instance` | PR (reviewed, versioned) |
| What the app may access | `resources/app.yml` → app `resources` | serving endpoints, warehouse, UC tables/volume, Lakebase DB | PR |
| App runtime behavior (same in all environments) | `app.yaml` → `env` | `HISTORY_TURNS`, `NUM_RESULTS`, `ASSISTANT_NAME` | PR |
| Values injected per environment | `app.yaml` `valueFrom: <resource>` | `VS_INDEX` ← `vector-index`, `SQL_WAREHOUSE_ID` ← `sql-warehouse` | automatic |
| Deployment identity and workspace | GitHub **environment variables** | `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID` | repo admin |
| Secrets | **none** (OIDC); fallback only: GitHub environment secret | `DATABRICKS_CLIENT_SECRET` | repo admin |
| Local development | `.env` (git-ignored, from `.env.example`) | `DATABRICKS_TOKEN`, `LAKEBASE_INSTANCE` | you |

**Rules**
1. Environment-specific values are **bundle variables**, never hard-coded in code or `app.yaml`. The app fails fast if a required value is missing (`VS_INDEX`, `CITATIONS_VIEW`, `DOCUMENTS_VIEW`, `SQL_WAREHOUSE_ID`, page-image volume).
2. Anything the app accesses is an **app resource** (one permission each). `app.yaml` reads its name or ID with `valueFrom`, so the same source runs everywhere.
3. One deployment identity per environment, bound to a GitHub environment by its federation policy.
4. Overrides for a single run: `databricks bundle deploy -t staging --var="llm_endpoint=..."` or `BUNDLE_VAR_llm_endpoint=...`.

**Bundle variables**

| Variable | dev | staging | prod | Notes |
|---|---|---|---|---|
| `app_name` | rag-assistant-dev | rag-assistant-staging | rag-assistant | lowercase, ≤ 30 chars |
| `gold_schema` | steampunk.dev_kbdhunga_rag_gold | same | steampunk.rag_gold | ETL output |
| `page_image_volume` | steampunk.dev_kbdhunga_rag_silver.page_images | same | steampunk.rag_silver.page_images | |
| `lakebase_instance` | rag-memory-dev | rag-memory-dev | rag-memory-prod | |
| `lakebase_database` | databricks_postgres | rag_staging | rag_prod | one database per environment |
| `llm_endpoint` / `helper_llm_endpoint` | databricks-claude-sonnet-5-5 / databricks-claude-haiku-5-5 | same | same | |
| `warehouse_id` | looked up by name *Serverless Starter Warehouse* | same | same | |
| `app_users_group` / `app_admins_group` | users / admins | same | same | narrow these for prod |
| `deployer_sp` | — | set by CI | set by CI | pins the bundle folder to the deployer |

---

# Part D: Operating it

| Task | How |
|---|---|
| Ship a change | PR → review → merge → staging deploys automatically → publish a release → approve → prod |
| Roll back prod | Publish a release from the previous good tag (or **Run workflow → prod** on that tag). The bundle is declarative, so the old version is restored as-is |
| Change a setting | Edit `app.yaml` (behavior) or `databricks.yml` (per-env), open a PR. Never change deployed apps by hand: the next deploy would overwrite it |
| Database schema change | Add `src/rag_app/store/migrations/002_<name>.sql` (idempotent DDL). The pipeline applies it before the new app version starts. Keep migrations backward compatible (add columns, don't rename) so the old version keeps working during rollout |
| Logs | Workspace → **Compute → Apps → rag-assistant → Logs** (`x-request-id` in every log line) |
| Who did what | **Apps → Deployments** lists every deployment with its commit. GitHub Actions has the full run history |
| Cost | Apps bill while running: stop staging when idle (`databricks apps stop rag-assistant-staging`). Lakebase CU_1 runs continuously; size prod by load |
| Rotate credentials | Nothing to rotate with OIDC. With the fallback secret: generate a new one, update GitHub, delete the old one |

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Check configuration and identity` fails: no `DATABRICKS_HOST` | Variables are missing on the GitHub **environment** (A7), not just the repo |
| OIDC: `invalid subject` / `audience` | Federation policy subject must be `repo:DhunganaKB/databricks-rag-assistant:environment:<env>`, audience exactly as in the table at the top |
| `Folder Users is protected` | Someone deployed staging/prod as a person; only the pipeline identity can (by design) |
| Bundle deploy: permission denied on an app resource | The deployer lacks a grant from A5 (warehouse *Can manage*, serving endpoint, Lakebase *Can manage*, UC `MANAGE`) |
| `post_deploy`: permission denied to create schema | A6 bootstrap not run for that environment's database |
| App starts but 500 on chat | **Apps → Logs**. Usually a missing UC grant (`post_deploy` prints the grants it made) |
| Evaluation gate fails | Download the `eval-report-<env>` artifact; failing questions include the judge's reason |
