#!/usr/bin/env bash
# Try each model candidate in order until one serves (or, in warm mode, warm
# them all). A candidate that fails to download/compile/serve is logged and
# skipped, so the chain self-heals around an unsupported GGUF or a flaky model.
#
# Env:
#   CANDIDATES_FILE   JSON array of candidates (from select-model.sh)
#   MAX_PORT          serve port (default 8000)
#   SERVE_TIMEOUT     seconds to wait for /v1/health (default 1800)
#   GGUF_DIR          where to cache downloaded GGUF weights
#   STOP_AT_FIRST     true: stop and keep serving the first that works (review);
#                     false: warm every candidate then exit (cache warmer)
#   GITHUB_OUTPUT     optional; served_model/served_quant/served_ok are written
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CANDIDATES_FILE="${CANDIDATES_FILE:?CANDIDATES_FILE required}"
MAX_PORT="${MAX_PORT:-8000}"
SERVE_TIMEOUT="${SERVE_TIMEOUT:-1800}"
GGUF_DIR="${GGUF_DIR:-${MODULAR_MAX_CACHE_DIR:-/tmp}/gguf}"
STOP_AT_FIRST="${STOP_AT_FIRST:-true}"

mkdir -p "$GGUF_DIR"

if [[ ! -s "$CANDIDATES_FILE" ]]; then
  echo "::error::No candidates file at $CANDIDATES_FILE"
  exit 1
fi
count=$(jq 'length' "$CANDIDATES_FILE")
if [[ "$count" -eq 0 ]]; then
  echo "::error::Candidate chain is empty (nothing fit RAM / all gated)."
  exit 1
fi

set_output() {
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "$1=$2" >> "$GITHUB_OUTPUT"
  fi
}

warmed=0
for (( i = 0; i < count; i++ )); do
  id=$(jq -r ".[$i].id" "$CANDIDATES_FILE")
  quant=$(jq -r ".[$i].quantization // \"float32\"" "$CANDIDATES_FILE")
  conc=$(jq -r ".[$i].concurrency // 2" "$CANDIDATES_FILE")
  gguf_repo=$(jq -r ".[$i].gguf_repo // empty" "$CANDIDATES_FILE")
  gguf_file=$(jq -r ".[$i].gguf_file // empty" "$CANDIDATES_FILE")
  tool_parser=$(jq -r ".[$i].tool_parser // empty" "$CANDIDATES_FILE")
  reasoning_parser=$(jq -r ".[$i].reasoning_parser // empty" "$CANDIDATES_FILE")

  echo "::group::Candidate $((i + 1))/${count}: ${id} (${quant})"

  weight_path=""
  if [[ -n "$gguf_repo" && -n "$gguf_file" ]]; then
    if ! weight_path=$(bash "${SCRIPT_DIR}/download-gguf.sh" "$gguf_repo" "$gguf_file" "$GGUF_DIR"); then
      echo "Skipping ${id}: GGUF download failed."
      echo "::endgroup::"
      continue
    fi
    echo "Using local weights: ${weight_path}"
  fi

  mapfile -d '' -t args < <(bash "${SCRIPT_DIR}/max-model-cli.sh" \
    "$id" "$quant" "$weight_path" "$tool_parser" "$reasoning_parser")

  log="/tmp/max-serve-${i}.log"
  : > "$log"
  # --allow-extra-request-fields (MAX >= 26.4): drop unknown top-level request
  # fields with a warning instead of 400, so a stray client param never fails a
  # whole review.
  nohup max serve "${args[@]}" --allow-extra-request-fields >> "$log" 2>&1 &
  pid=$!

  if bash "${SCRIPT_DIR}/wait-for-max-serve.sh" \
    "http://127.0.0.1:${MAX_PORT}/v1/health" "$pid" "$log" "$SERVE_TIMEOUT"; then
    echo "Candidate ${id} is serving (pid ${pid})."
    warmed=$((warmed + 1))
    if [[ "$STOP_AT_FIRST" == "true" ]]; then
      echo "$pid" > /tmp/max-serve.pid
      set_output served_model "$id"
      set_output served_quant "$quant"
      set_output served_concurrency "$conc"
      set_output served_ok "true"
      if bash "${SCRIPT_DIR}/is-qwen25-max-model.sh" "$id"; then
        echo "Qwen 2.5 on MAX: starting Hermes tool-call proxy."
        bash "${SCRIPT_DIR}/start-qwen25-max-tool-call-proxy.sh"
      else
        set_output llm_port "$MAX_PORT"
        set_output qwen25_max_tool_call_proxy "false"
      fi
      echo "::endgroup::"
      echo "Serving ${id} for review."
      exit 0
    fi
    # Warm mode: stop this one and move on so the next can compile into cache.
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    sleep 3  # let the port free up before the next candidate binds it
    echo "::endgroup::"
    continue
  fi

  echo "::error title=MAX serve failed::${id} did not become ready (see log tail below)."
  echo "Candidate ${id} failed to become ready; tail of log:"
  tail -n 120 "$log" || true
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  sleep 3
  echo "::endgroup::"
done

if [[ "$STOP_AT_FIRST" == "true" ]]; then
  set_output served_ok "false"
  echo "::error::No candidate served successfully (tried ${count})."
  exit 1
fi

echo "Warmed ${warmed}/${count} candidates."
[[ "$warmed" -gt 0 ]] || { echo "::error::Warmed 0 candidates."; exit 1; }
