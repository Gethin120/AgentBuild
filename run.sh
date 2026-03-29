#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_MODULE="app.agent"

# Load environment variables from local files when present.
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  # shellcheck disable=SC1091
  set -a; source "$SCRIPT_DIR/.env"; set +a
fi
if [[ -f "$SCRIPT_DIR/.env.local" ]]; then
  # shellcheck disable=SC1091
  set -a; source "$SCRIPT_DIR/.env.local"; set +a
fi

LMSTUDIO_BASE_URL="${LMSTUDIO_BASE_URL:-http://127.0.0.1:1234/v1}"
MODEL_NAME="${MODEL_NAME:-qwen/qwen3.5-9b}"
AMAP_WEB_SERVICE_KEY="${AMAP_WEB_SERVICE_KEY:-}"

if [[ ! -f "$SCRIPT_DIR/app/agent.py" ]]; then
  echo "Error: missing module file: $SCRIPT_DIR/app/agent.py" >&2
  exit 1
fi

if [[ -z "$AMAP_WEB_SERVICE_KEY" ]]; then
  echo "Error: AMAP_WEB_SERVICE_KEY is not set." >&2
  echo "Example: export AMAP_WEB_SERVICE_KEY='your_key'" >&2
  exit 1
fi

if [[ $# -lt 1 ]]; then
  cat >&2 <<'EOF'
Usage:
  ./run.sh "你的自然语言请求"

Example:
  ./run.sh "我从上海虹桥火车站出发，朋友在上海世纪大道地铁站，我们一起去上海迪士尼乐园。自动找会合点，朋友公交不超过120分钟，我最多绕路90分钟，最多等待45分钟。"
EOF
  exit 1
fi

USER_REQUEST="$1"

if [[ -x "/opt/miniconda3/bin/conda" ]]; then
  /opt/miniconda3/bin/conda run -n llm_local python -m "$PY_MODULE" \
    --user-request "$USER_REQUEST" \
    --lmstudio-base-url "$LMSTUDIO_BASE_URL" \
    --model "$MODEL_NAME" \
    --progress \
    --show-diagnostics \
    --print-intent \
    --retry-max-attempts 2 \
    --planner-timeout-sec 120 \
    --planner-max-retries 2
else
  python3 -m "$PY_MODULE" \
    --user-request "$USER_REQUEST" \
    --lmstudio-base-url "$LMSTUDIO_BASE_URL" \
    --model "$MODEL_NAME" \
    --progress \
    --show-diagnostics \
    --print-intent \
    --retry-max-attempts 2 \
    --planner-timeout-sec 120 \
    --planner-max-retries 2
fi
