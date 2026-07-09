#!/usr/bin/env bash
# Start qwen25-max-tool-call-proxy.py in front of a running max serve instance.
#
# Only used when the served model is Qwen 2.5 on local MAX (see is-qwen25-max-model.sh).
#
# Env:
#   MAX_PORT          upstream MAX port (default 8000)
#   LLM_PROXY_PORT    proxy listen port (default MAX_PORT + 1)
#   GITHUB_OUTPUT     optional; writes llm_port and qwen25_max_tool_call_proxy
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAX_PORT="${MAX_PORT:-8000}"
LLM_PROXY_PORT="${LLM_PROXY_PORT:-$((MAX_PORT + 1))}"
LOG="/tmp/qwen25-max-tool-call-proxy.log"
PID_FILE="/tmp/qwen25-max-tool-call-proxy.pid"

set_output() {
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "$1=$2" >> "$GITHUB_OUTPUT"
  fi
}

if ! curl -fsS "http://127.0.0.1:${MAX_PORT}/v1/health" >/dev/null 2>&1; then
  echo "::error::MAX is not healthy on port ${MAX_PORT}; cannot start Qwen2.5 tool-call proxy."
  exit 1
fi

nohup python3 "${SCRIPT_DIR}/qwen25-max-tool-call-proxy.py" \
  --upstream "http://127.0.0.1:${MAX_PORT}" \
  --port "$LLM_PROXY_PORT" >> "$LOG" 2>&1 &
proxy_pid=$!
echo "$proxy_pid" > "$PID_FILE"

deadline=$((SECONDS + 30))
while (( SECONDS < deadline )); do
  if ! kill -0 "$proxy_pid" 2>/dev/null; then
    echo "::error::Qwen2.5 MAX tool-call proxy exited during startup." >&2
    tail -n 40 "$LOG" >&2 || true
    exit 1
  fi
  if curl -fsS "http://127.0.0.1:${LLM_PROXY_PORT}/v1/health" >/dev/null 2>&1; then
    echo "Qwen2.5 MAX tool-call proxy ready on port ${LLM_PROXY_PORT} (pid ${proxy_pid})."
    set_output llm_port "$LLM_PROXY_PORT"
    set_output qwen25_max_tool_call_proxy "true"
    exit 0
  fi
  sleep 1
done

echo "::error::Timed out waiting for Qwen2.5 MAX tool-call proxy on port ${LLM_PROXY_PORT}." >&2
tail -n 40 "$LOG" >&2 || true
kill "$proxy_pid" 2>/dev/null || true
exit 1
