#!/usr/bin/env bash
# Smoke-test an OpenAI-compatible chat completions endpoint (MAX or Qwen2.5 proxy).
#
# Usage: verify-llm-endpoint.sh <base_v1_url> <model_id> [auth_token]
# Example: verify-llm-endpoint.sh http://127.0.0.1:8001/v1 Qwen/Qwen2.5-1.5B-Instruct
set -euo pipefail

BASE="${1:?usage: verify-llm-endpoint.sh <base_v1_url> <model_id> [auth_token]}"
MODEL="${2:?missing model id}"
TOKEN="${3:-local-max}"
ENDPOINT="${BASE%/}/chat/completions"
BODY_FILE=/tmp/llm-smoke-body.json
HDR_FILE=/tmp/llm-smoke-headers.txt

echo "LLM smoke test: POST ${ENDPOINT} (model=${MODEL})"
http_code=$(curl --no-location --max-time 180 -sS -o "$BODY_FILE" -D "$HDR_FILE" -w "%{http_code}" \
  -X POST "$ENDPOINT" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "Expect:" \
  -d "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":8,\"stream\":false}") || {
    echo "::error::LLM smoke test curl failed for ${ENDPOINT}" >&2
    exit 1
  }

ctype=$(awk -F': ' 'tolower($1)=="content-type"{print $2; exit}' "$HDR_FILE" | tr -d '\r' || true)
echo "LLM smoke test response: HTTP ${http_code}, Content-Type: ${ctype:-unknown}"

if [[ "$http_code" -lt 200 || "$http_code" -ge 300 ]]; then
  echo "::error::LLM smoke test got HTTP ${http_code} from ${ENDPOINT}" >&2
  head -c 2000 "$BODY_FILE" >&2 || true
  exit 1
fi

if [[ ! -s "$BODY_FILE" ]]; then
  echo "::error::LLM smoke test returned an empty body from ${ENDPOINT}" >&2
  exit 1
fi

first_char=$(head -c 1 "$BODY_FILE")
if [[ "$first_char" != "{" ]]; then
  echo "::error::LLM smoke test expected JSON but got non-JSON body (often /metrics or a redirect target):" >&2
  head -c 2000 "$BODY_FILE" >&2 || true
  exit 1
fi

if ! jq -e '.choices[0].message' "$BODY_FILE" >/dev/null 2>&1; then
  echo "::error::LLM smoke test JSON missing .choices[0].message:" >&2
  head -c 2000 "$BODY_FILE" >&2 || true
  exit 1
fi

echo "LLM smoke test OK."
