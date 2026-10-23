#!/usr/bin/env bash
# Create a local .env from .env.example, taking the workspace PAT from ../.env (key DAP).
# .env is git-ignored. Re-running never overwrites an existing .env.
#   scripts/bootstrap_env.sh
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f .env ]]; then
  echo ".env already exists - leaving it unchanged"; exit 0
fi
token=""
if [[ -f ../.env ]]; then
  token="$(grep -E '^DAP=' ../.env | head -1 | cut -d= -f2- | tr -d '"'"'"' \r\n')"
fi
if [[ -z "$token" ]]; then
  echo "No DAP token found in ../.env - fill DATABRICKS_TOKEN in .env by hand."
fi
sed "s|^DATABRICKS_TOKEN=.*|DATABRICKS_TOKEN=${token}|" .env.example > .env
chmod 600 .env
if [[ -n "$token" ]]; then echo "created .env (token found)"; else echo "created .env (token missing)"; fi
