# Deploy the app to your Databricks workspace (one environment)

**Goal:** every push to `main` on GitHub automatically deploys the app to your current Azure Databricks workspace, as a Databricks App named **`rag-assistant-dev`**.

## The two service principals (read this first)

A *service principal* is a non-human account in Databricks, an identity for software instead of a person. This setup uses **two**:

| | 1. Deployment service principal | 2. App service principal |
|---|---|---|
| Name | `sp-rag-deployer` | created automatically, e.g. `app-xxxx rag-assistant-dev` |
| Who creates it | **you**, once, in the Databricks UI (step 1) | **Databricks**, when the app is first deployed |
| Who uses it | **GitHub Actions**, to sign in to Databricks and deploy | **the running app**, to read data, call the models and use Lakebase |
| Why not your own account | The pipeline must not depend on your password or session, should have only the rights it needs, and should appear in audit logs as "the pipeline", not you | The app gets exactly the access declared in `resources/app.yml`, nothing more |
| Credentials | Application ID + OAuth secret, stored in GitHub (step 6) | none to manage: Databricks handles them |

You create #1. GitHub uses #1 to create the app, which creates #2. The pipeline then uses #1 to grant #2 its data access.

## What you do where

| Where | What | Step |
|---|---|---|
| Databricks UI | Create the deployment service principal and its secret | 1, 2 |
| Databricks UI | Give it permissions (SQL statements + 2 permission dialogs) | 3, 4 |
| Your terminal (once) | Create the app's Lakebase database | 5 |
| GitHub UI | Create environment `dev` with 2 variables + 1 secret | 6 |
| GitHub Actions (automatic) | Test, deploy the app, grants, Lakebase tables, start, smoke test, evaluation | 7 |

---

## Step 1: Create the deployment service principal (Databricks UI)

1. Open the workspace: `https://adb-7405616448132971.11.azuredatabricks.net`
2. Click your user icon (top right) → **Settings** → **Identity and access** → **Service principals** → **Manage** → **Add service principal** → **Add new**.
3. Name: `sp-rag-deployer`. Management: **Databricks managed**. Click **Add**.
4. Open `sp-rag-deployer`:
   - Copy the **Application ID** (a UUID like `1a2b3c4d-...`). You need it in steps 3, 5 and 6.
   - **Configurations** tab: tick **Workspace access** only. No admin rights.

## Step 2: Create its secret (Databricks UI)

Still on `sp-rag-deployer` → **Secrets** tab → **Generate secret** → lifetime e.g. 365 days → **Generate**.

Copy the **Secret** now; it is shown only once. Don't save it in any file. It goes straight into GitHub in step 6.

## Step 3: Give it access to the data (Databricks UI → SQL Editor)

Open **SQL Editor**, select the *Serverless Starter Warehouse*, replace `<APPLICATION_ID>` and run:

```sql
GRANT USE CATALOG ON CATALOG steampunk TO `<APPLICATION_ID>`;
GRANT USE SCHEMA, SELECT, MANAGE ON SCHEMA steampunk.dev_kbdhunga_rag_gold   TO `<APPLICATION_ID>`;
GRANT USE SCHEMA, READ VOLUME, MANAGE ON SCHEMA steampunk.dev_kbdhunga_rag_silver TO `<APPLICATION_ID>`;
```

Why: the deployer reads the gold data during the smoke test, and `MANAGE` lets it pass read access on to the **app** service principal. The deployer cannot modify the data.

## Step 4: Give it access to the warehouse and Lakebase (Databricks UI)

| Object | Where in the UI | Add `sp-rag-deployer` with |
|---|---|---|
| SQL warehouse *Serverless Starter Warehouse* | **SQL Warehouses** → the warehouse → **Permissions** | **Can manage** |
| Lakebase instance `rag-memory-dev` | **Compute** → **Lakebase** (database instances) → `rag-memory-dev` → **Permissions** | **Can manage** |
| Endpoints `databricks-claude-sonnet-5-5` and `databricks-claude-haiku-5-5` | **Serving** → endpoint → **Permissions** (only if that button exists; foundation models are usually open to all users) | **Can query** |

Why: the app is attached to this warehouse and this Lakebase instance, and only an identity that manages them may attach and share them.

## Step 5: Create the app's Lakebase database (your terminal, once)

The deployed app keeps its conversations in its own database, `rag_app`, separate from the one you use when running locally. Run from the `RAG` folder (you're the instance owner):

```bash
uv run --env-file .env python scripts/post_deploy.py --instance rag-memory-dev --database rag_app --bootstrap-for <APPLICATION_ID>
```

Expected output: `created database rag_app` and `granted CONNECT, CREATE on database rag_app to <APPLICATION_ID>`. On its first run the pipeline creates the tables inside it (as `sp-rag-deployer`, which then owns them).

## Step 6: Create the GitHub environment with the credentials (GitHub UI)

GitHub repo `DhunganaKB/databricks-rag-assistant` → **Settings** → **Environments** → **New environment** → name **`dev`** → **Configure environment**:

| Type | Name | Value |
|---|---|---|
| Environment **variable** | `DATABRICKS_HOST` | `https://adb-7405616448132971.11.azuredatabricks.net` |
| Environment **variable** | `DATABRICKS_CLIENT_ID` | the Application ID from step 1 |
| Environment **secret** | `DATABRICKS_CLIENT_SECRET` | the secret from step 2 |

Also on that page: **Deployment branches and tags** → **Selected branches** → add `main`.

- **Why an environment, not repository secrets:** the secret is only released to jobs that declare `environment: dev`, and only for the `main` branch. A pull request from another branch can't read it.
- **Variable vs secret:** host and client ID aren't sensitive (variables, readable in logs). The client secret is (a secret: masked, write-only).

## Step 7: Deploy (GitHub Actions)

Push or merge to `main`:
```bash
git push
```
Or run it by hand: GitHub → **Actions** → **deploy** → **Run workflow**.

What runs automatically (`.github/workflows/deploy.yml` → `_deploy.yml`):

| # | Step | What it does |
|---|---|---|
| 1 | ci | lint, format check, 48 tests, lockfile check: nothing deploys if these fail |
| 2 | Check configuration and identity | fails clearly if a variable or the secret is missing; prints `deploying as: <APPLICATION_ID>` |
| 3 | Validate bundle | checks `databricks.yml` + `resources/app.yml` |
| 4 | Deploy bundle | creates or updates the app `rag-assistant-dev` and its resources; Databricks creates the app service principal |
| 5 | Unity Catalog grants, Lakebase migrations and app grants | gives the app SP read access to the gold tables and volume; creates the `rag_memory` tables in `rag_app`; gives the app SP read/write (no DDL) on them |
| 6 | Deploy and start the app | uploads the code, installs `requirements.txt`, starts `uvicorn` |
| 7 | Smoke test | 20 checks against the deployed URL, then cleans up its data |
| 8 | Evaluation gate | 25 questions; fails below 90% correct |
| 9 | Summary | app URL and evaluation result on the run page; report attached as an artifact |

## Step 8: Open the app

Databricks → **Compute** → **Apps** → **rag-assistant-dev** → open the URL (also printed in the run summary). Anyone in the workspace group `users` can use it.

---

## How settings flow (environment variables)

```
GitHub environment "dev"                         databricks.yml (variables)           app.yaml (env)
──────────────────────────                       ─────────────────────────            ──────────────
DATABRICKS_HOST          ┐                       app_name = rag-assistant-dev         VS_INDEX      ← valueFrom: vector-index
DATABRICKS_CLIENT_ID     ├─▶ pipeline signs in   gold_schema = …rag_gold        ─┐    CITATIONS_VIEW← valueFrom: citations-view
DATABRICKS_CLIENT_SECRET ┘   as sp-rag-deployer  page_image_volume = …          ─┼─▶  SQL_WAREHOUSE_ID ← valueFrom: sql-warehouse
                                                 lakebase_instance/database     ─┤    LLM_ENDPOINT  ← valueFrom: llm
                                                 llm_endpoint, warehouse_id …   ─┘    PGHOST/PGUSER/PGDATABASE ← set by the database resource
                                                        │ resources/app.yml             HISTORY_TURNS=5, NUM_RESULTS=8 … (fixed values)
                                                        └─ app resources = permissions + values for valueFrom
```

| To change… | Edit | Then |
|---|---|---|
| Which data, Lakebase, model or app name | `databricks.yml` → `variables` defaults | push to `main` |
| App behavior (history turns, results, name shown) | `app.yaml` → `value:` | push to `main` |
| Who can open the app | `databricks.yml` → `app_users_group` | push to `main` |
| Pipeline credentials | GitHub → Settings → Environments → `dev` | re-run the workflow |
| Local development settings | `.env` (never committed) | `make run` |

Nothing environment-specific is hard-coded in Python: the app reads environment variables at startup and stops with a clear error if one is missing.

## If something fails

| Message | Fix |
|---|---|
| `has no DATABRICKS_HOST variable` / `CLIENT_SECRET secret` | Step 6: values must be on the **environment** `dev`, not the repo |
| `invalid_client` / 401 during sign-in | Wrong Application ID or secret (step 1/2); generate a new secret and update GitHub |
| `Folder Users is protected` | Deploying as a person instead of the pipeline: deploy only through GitHub Actions |
| Permission denied while deploying the app | A step 3 or 4 permission is missing |
| `permission denied to create schema` in step 5 of the run | Step 5 (bootstrap) not run, or run with a different Application ID |
| App starts, but chat returns 500 | Databricks → Apps → rag-assistant-dev → **Logs** |
| Evaluation gate fails | Open the run → artifact `eval-report-dev` → failing questions with the judge's reason |

## Optional, later: no secret at all
Instead of the OAuth secret, GitHub can sign in with a short-lived OIDC token. In the account console (`https://accounts.azuredatabricks.net`), add a **federation policy** to `sp-rag-deployer`:
- Issuer: `https://token.actions.githubusercontent.com`
- Subject: `repo:DhunganaKB/databricks-rag-assistant:environment:dev`
- Audience: `https://adb-7405616448132971.11.azuredatabricks.net/oidc/v1/token`

Then add variable `DATABRICKS_AUTH_TYPE=github-oidc` to the `dev` environment and delete the secret. The workflow already supports it.
