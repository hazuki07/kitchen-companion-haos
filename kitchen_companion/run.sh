#!/usr/bin/with-contenv bashio
set -euo pipefail

export PYTHONUNBUFFERED=1
export COOKING_ASSISTANT_DB=/data/recipes.sqlite3
export COOKING_ASSISTANT_MODE="$(bashio::config mode)"
export COOKING_ASSISTANT_TIMER_SCRIPT_ENTITY_ID="$(bashio::config timer_script_entity_id)"
export COOKING_ASSISTANT_SHOPPING_LIST_ENTITY_ID="$(bashio::config shopping_list_entity_id)"
export COOKING_ASSISTANT_TASK_LIST_ENTITY_ID="$(bashio::config task_list_entity_id)"
export COOKING_ASSISTANT_JEV_ENABLED="$(bashio::config jev_enabled)"
export TYPESAFE_API_KEY="$(bashio::config jev_api_key)"
export COOKING_ASSISTANT_EXTERNAL_CONFIG=/config/integrations.json
export TRUST_HOME_ASSISTANT_INGRESS=1

exec python3 -m app.server --host 0.0.0.0 --port 8099
