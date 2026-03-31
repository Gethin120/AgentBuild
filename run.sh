#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -f "$SCRIPT_DIR/.env" ]]; then
  set -a; source "$SCRIPT_DIR/.env"; set +a
fi
if [[ -f "$SCRIPT_DIR/.env.local" ]]; then
  set -a; source "$SCRIPT_DIR/.env.local"; set +a
fi

if [[ -z "${AMAP_WEB_SERVICE_KEY:-}" ]]; then
  echo "Error: AMAP_WEB_SERVICE_KEY is not set." >&2
  exit 1
fi

export LANGSMITH_TRACING="${LANGSMITH_TRACING:-false}"

if [[ -x "/opt/miniconda3/envs/llm_local/bin/python" ]]; then
  exec /opt/miniconda3/envs/llm_local/bin/python -m app.chat_cli "$@"
fi

if [[ -x "/opt/miniconda3/bin/conda" ]]; then
  exec /opt/miniconda3/bin/conda run --no-capture-output -n llm_local python -m app.chat_cli "$@"
fi

exec python3 -m app.chat_cli "$@"
