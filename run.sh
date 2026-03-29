#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="$SCRIPT_DIR/agent_local.py"

LMSTUDIO_BASE_URL="${LMSTUDIO_BASE_URL:-http://127.0.0.1:1234/v1}"
MODEL_NAME="${MODEL_NAME:-qwen/qwen3.5-9b}"
AMAP_WEB_SERVICE_KEY="${AMAP_WEB_SERVICE_KEY:-}"

if [[ ! -f "$PY_SCRIPT" ]]; then
  echo "Error: missing script: $PY_SCRIPT" >&2
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

python3 "$PY_SCRIPT" \
  --user-request "$USER_REQUEST" \
  --lmstudio-base-url "$LMSTUDIO_BASE_URL" \
  --model "$MODEL_NAME" \
  --show-diagnostics \
  --print-intent
