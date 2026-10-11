#!/usr/bin/with-contenv bashio
set -euo pipefail

export PYTHONUNBUFFERED=1
export COOKING_ASSISTANT_DB=/data/recipes.sqlite3
export COOKING_ASSISTANT_MODE="$(bashio::config mode)"
export COOKING_ASSISTANT_TIMER_SCRIPT_ENTITY_ID="$(bashio::config timer_script_entity_id)"
export COOKING_ASSISTANT_EXTERNAL_CONFIG=/config/integrations.json
export TRUST_HOME_ASSISTANT_INGRESS=1

exec python3 -m app.server --host 0.0.0.0 --port 8099
