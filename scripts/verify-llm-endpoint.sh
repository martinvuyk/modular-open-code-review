#!/usr/bin/env bash
# Smoke-test the LLM endpoint OCR will call (MAX directly or Qwen2.5 proxy).
#
# Usage: verify-llm-endpoint.sh <base_v1_url> <model_id> [auth_token]
# Example: verify-llm-endpoint.sh http://127.0.0.1:8001/v1 Qwen/Qwen2.5-1.5B-Instruct
set -euo pipefail

BASE="${1:?usage: verify-llm-endpoint.sh <base_v1_url> <model_id> [auth_token]}"
MODEL="${2:?missing model id}"
TOKEN="${3:-local-max}"
ENDPOINT="${BASE%/}/chat/completions"

echo "LLM smoke test: POST ${ENDPOINT} (model=${MODEL})"
resp=$(curl -fsS --max-time 180 -X POST "$ENDPOINT" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":8,\"stream\":false}" \
  2>/tmp/llm-smoke-stderr.log) || {
    echo "::error::LLM smoke test failed for ${ENDPOINT}" >&2
    cat /tmp/llm-smoke-stderr.log >&2 || true
    exit 1
  }

if ! jq -e '.choices[0].message' >/dev/null 2>&1 <<<"$resp"; then
  echo "::error::LLM smoke test returned unexpected JSON:" >&2
  echo "$resp" >&2
  exit 1
fi

echo "LLM smoke test OK."
