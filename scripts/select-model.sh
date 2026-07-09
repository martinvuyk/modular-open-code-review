#!/usr/bin/env bash
# Select an ordered MAX model fallback chain and OCR concurrency from the token
# estimate and available RAM.
#
# The config defines an ordered list of candidates (best reviewer first, safest
# last). We drop candidates that are gated (unless ALLOW_GATED_MODELS=true) or
# that don't fit the runner's free RAM, then emit the survivors — in order — to
# /tmp/ocr-candidates.json. setup-modular-max tries each in turn until one
# actually serves, so a candidate that fails to load (e.g. an unsupported GGUF)
# transparently falls through to the next.
#
# RAM fit is estimated from each model's parameter count times the bytes-per-
# weight of its encoding, plus a safety factor and fixed overhead. MAX only
# sizes the KV cache against available memory; it does not gate the weight load,
# so we pre-flight it here rather than letting the runner OOM-kill the compile.
set -euo pipefail

# Force a dot decimal separator so awk parses config floats (e.g. 1.4) the same
# way regardless of the runner locale.
export LC_ALL=C

TOKENS_ENV="${1:-/tmp/ocr-tokens.env}"
CONFIG="${2:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/config/models.cpu.json}"
CANDIDATES_OUT="${3:-/tmp/ocr-candidates.json}"
MODEL_OVERRIDE="${MODEL_OVERRIDE:-}"
MODEL_OVERRIDE_QUANT="${MODEL_OVERRIDE_QUANT:-}"
# When an external LLM URL is used there is no local model to load, so skip all
# RAM gating (the runner never holds the weights).
EXTERNAL_LLM="${EXTERNAL_LLM:-false}"
# Non-Apache / license-gated candidates (e.g. Llama) are opt-in only.
ALLOW_GATED_MODELS="${ALLOW_GATED_MODELS:-false}"

if [[ ! -f "$TOKENS_ENV" ]]; then
  echo "Missing token estimate file: $TOKENS_ENV" >&2
  exit 1
fi

# Written by estimate-review-tokens.sh (KEY=value lines).
BUNDLE_COUNT=$(grep -m1 '^BUNDLE_COUNT=' "$TOKENS_ENV" | cut -d= -f2-)
ESTIMATED_TOKENS=$(grep -m1 '^ESTIMATED_TOKENS=' "$TOKENS_ENV" | cut -d= -f2-)

max_tokens=$(jq -r '.thresholds.max_tokens' "$CONFIG")
if [[ -n "${MAX_ESTIMATED_TOKENS:-}" ]]; then
  max_tokens="$MAX_ESTIMATED_TOKENS"
fi

safety_factor=$(jq -r '.ram_estimate.safety_factor // 1.4' "$CONFIG")
base_overhead_gb=$(jq -r '.ram_estimate.base_overhead_gb // 2' "$CONFIG")

mem_avail_kb=$(awk '/MemAvailable:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)
free_ram_gb=$(( mem_avail_kb / 1024 / 1024 ))

# Parameter count: prefer the config's declared value, then the HF API, then a
# "<n>B" parse of the model id (e.g. Qwen2.5-1.5B -> 1.5e9).
model_param_count() {
  local id="$1" declared="${2:-}" total=""
  if [[ -n "$declared" && "$declared" =~ ^[0-9]+$ && "$declared" -gt 0 ]]; then
    printf '%s' "$declared"; return
  fi
  total=$(curl -fsSL --max-time 8 "https://huggingface.co/api/models/${id}" 2>/dev/null \
    | jq -r '.safetensors.total // empty' 2>/dev/null || true)
  if [[ -z "$total" || ! "$total" =~ ^[0-9]+$ ]]; then
    local b
    b=$(printf '%s' "$id" | grep -oiE '[0-9]+(\.[0-9]+)?B' | head -1 | tr -d 'Bb' || true)
    [[ -n "$b" ]] && total=$(awk -v b="$b" 'BEGIN{printf "%.0f", b*1000000000}')
  fi
  printf '%s' "${total:-0}"
}

# Bytes per weight for the target encoding (config-driven, default float32=4).
dtype_bytes() {
  local q="${1:-float32}" v
  v=$(jq -r --arg q "$q" '.ram_estimate.dtype_bytes[$q] // empty' "$CONFIG")
  printf '%s' "${v:-4}"
}

# Peak RAM (GiB, rounded up) = params * bytes * safety_factor + overhead.
required_ram_gib() {
  local params="$1" quant="$2" bytes
  bytes=$(dtype_bytes "$quant")
  awk -v p="$params" -v b="$bytes" -v sf="$safety_factor" -v ov="$base_overhead_gb" \
    'BEGIN { r = (p * b / 1073741824) * sf + ov; printf "%d", (r == int(r)) ? r : int(r) + 1 }'
}

SKIP_REVIEW=false
REASON=""
REQUIRED_RAM_GB=0
OCR_CONCURRENCY=3

if [[ "${ESTIMATED_TOKENS:-0}" -gt "$max_tokens" ]]; then
  SKIP_REVIEW=true
  REASON="Estimated tokens (${ESTIMATED_TOKENS}) exceed max (${max_tokens})"
fi

emit_env() {
  {
    echo "ESTIMATED_TOKENS=${ESTIMATED_TOKENS}"
    echo "MAX_MODEL=${MAX_MODEL:-}"
    echo "MAX_QUANT=${MAX_QUANT:-}"
    echo "OCR_CONCURRENCY=${OCR_CONCURRENCY}"
    echo "FREE_RAM_GB=${free_ram_gb}"
    echo "REQUIRED_RAM_GB=${REQUIRED_RAM_GB}"
    echo "SKIP_REVIEW=${SKIP_REVIEW}"
    echo "SKIP_REASON=${REASON}"
    echo "CANDIDATES_FILE=${CANDIDATES_OUT}"
  } | tee /tmp/ocr-model.env
}

# External API: no local weights, so no RAM estimate or fit check. The model
# name is the override if given, else the first candidate id (as a label for
# llm.model). No candidate chain is needed.
if [[ "$EXTERNAL_LLM" == "true" ]]; then
  if [[ -n "$MODEL_OVERRIDE" ]]; then
    MAX_MODEL="$MODEL_OVERRIDE"
  else
    MAX_MODEL=$(jq -r '.candidates[0].id' "$CONFIG")
  fi
  MAX_QUANT="$MODEL_OVERRIDE_QUANT"
  echo '[]' > "$CANDIDATES_OUT"
  emit_env
  echo "External LLM: model label ${MAX_MODEL} (no local MAX, RAM gating skipped)"
  exit 0
fi

# Explicit override: a single-candidate chain using the requested model.
if [[ -n "$MODEL_OVERRIDE" ]]; then
  q="${MODEL_OVERRIDE_QUANT:-float32}"
  params=$(model_param_count "$MODEL_OVERRIDE" "")
  REQUIRED_RAM_GB=$(required_ram_gib "$params" "$q")
  jq -n --arg id "$MODEL_OVERRIDE" --arg q "$q" --argjson c 3 \
    '[{id:$id, quantization:$q, concurrency:$c}]' > "$CANDIDATES_OUT"
  MAX_MODEL="$MODEL_OVERRIDE"
  MAX_QUANT="$q"
  if [[ "$free_ram_gb" -lt "$REQUIRED_RAM_GB" ]]; then
    REASON="Override ${MAX_MODEL} (${q}) needs ~${REQUIRED_RAM_GB}GB but only ${free_ram_gb}GB free; may OOM"
    echo "WARNING: $REASON" >&2
  fi
  emit_env
  echo "Override chain: ${MAX_MODEL} (${q}), need~${REQUIRED_RAM_GB}GB, free=${free_ram_gb}GB"
  exit 0
fi

# Local MAX: build the RAM-fitting, gating-aware ordered chain.
num=$(jq '.candidates | length' "$CONFIG")
CHAIN='[]'
SMALLEST_ID=""; SMALLEST_QUANT=""; SMALLEST_NEED=""
for (( i = 0; i < num; i++ )); do
  id=$(jq -r ".candidates[$i].id" "$CONFIG")
  quant=$(jq -r ".candidates[$i].quantization" "$CONFIG")
  gated=$(jq -r ".candidates[$i].gated // false" "$CONFIG")
  declared=$(jq -r ".candidates[$i].params // empty" "$CONFIG")

  if [[ "$gated" == "true" && "$ALLOW_GATED_MODELS" != "true" ]]; then
    echo "Skipping gated candidate ${id} (set allow_gated_models to enable)." >&2
    continue
  fi

  params=$(model_param_count "$id" "$declared")
  need=$(required_ram_gib "$params" "$quant")
  # Track the overall-smallest candidate so we can report it if nothing fits.
  if [[ -z "$SMALLEST_NEED" || "$need" -lt "$SMALLEST_NEED" ]]; then
    SMALLEST_ID="$id"; SMALLEST_QUANT="$quant"; SMALLEST_NEED="$need"
  fi
  if [[ "$free_ram_gb" -lt "$need" ]]; then
    echo "Candidate ${id} (${quant}) needs ~${need}GB, only ${free_ram_gb}GB free; skipping." >&2
    continue
  fi

  entry=$(jq -c ".candidates[$i] + {required_ram_gb: ${need}}" "$CONFIG")
  CHAIN=$(jq -c ". + [${entry}]" <<<"$CHAIN")
done

chain_len=$(jq 'length' <<<"$CHAIN")
if [[ "$chain_len" -eq 0 ]]; then
  # Nothing fits: surface the smallest candidate and skip the review.
  echo '[]' > "$CANDIDATES_OUT"
  MAX_MODEL="${SMALLEST_ID}"
  MAX_QUANT="${SMALLEST_QUANT}"
  OCR_CONCURRENCY=2
  REQUIRED_RAM_GB="${SMALLEST_NEED:-0}"
  SKIP_REVIEW=true
  REASON="No model fits: smallest (${SMALLEST_ID}) needs ~${SMALLEST_NEED}GB but only ${free_ram_gb}GB free"
  emit_env
  echo "No candidate fits ${free_ram_gb}GB free RAM; skipping review." >&2
  exit 0
fi

echo "$CHAIN" | jq '.' > "$CANDIDATES_OUT"

# Top candidate drives the summary / OCR model label; the serve step may end up
# using a later one if the top fails to load.
MAX_MODEL=$(jq -r '.[0].id' <<<"$CHAIN")
MAX_QUANT=$(jq -r '.[0].quantization' <<<"$CHAIN")
OCR_CONCURRENCY=$(jq -r '.[0].concurrency // 2' <<<"$CHAIN")
REQUIRED_RAM_GB=$(jq -r '.[0].required_ram_gb' <<<"$CHAIN")

max_bundles=$(jq -r '.thresholds.max_bundles_before_concurrency_cap' "$CONFIG")
if [[ "${BUNDLE_COUNT:-0}" -gt "$max_bundles" ]]; then
  OCR_CONCURRENCY=$(( OCR_CONCURRENCY > 2 ? 2 : OCR_CONCURRENCY ))
fi

emit_env
echo "Model chain (${chain_len}): $(jq -r '[.[].id] | join(" -> ")' <<<"$CHAIN")"
echo "Preferred: ${MAX_MODEL} (${MAX_QUANT}), concurrency=${OCR_CONCURRENCY}, need~${REQUIRED_RAM_GB}GB, free=${free_ram_gb}GB"
