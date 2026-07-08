#!/usr/bin/env bash
# Select MAX model and OCR concurrency from token estimate and available RAM.
#
# RAM fit is estimated from the model's real parameter count (Hugging Face API)
# times the bytes-per-weight of the target encoding, plus a safety factor and a
# fixed overhead. MAX itself only sizes the KV cache against available memory; it
# does not gate the weight load, so we pre-flight it here and downgrade tiers (or
# skip the review) rather than letting the runner OOM-kill the compile.
set -euo pipefail

# Force a dot decimal separator so awk parses config floats (e.g. 1.4) the same
# way regardless of the runner locale.
export LC_ALL=C

TOKENS_ENV="${1:-/tmp/ocr-tokens.env}"
CONFIG="${2:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/config/models.cpu.json}"
MODEL_OVERRIDE="${MODEL_OVERRIDE:-}"
# When an external LLM URL is used there is no local model to load, so skip all
# RAM gating (the runner never holds the weights).
EXTERNAL_LLM="${EXTERNAL_LLM:-false}"

if [[ ! -f "$TOKENS_ENV" ]]; then
  echo "Missing token estimate file: $TOKENS_ENV" >&2
  exit 1
fi

# Written by estimate-review-tokens.sh (KEY=value lines).
BUNDLE_COUNT=$(grep -m1 '^BUNDLE_COUNT=' "$TOKENS_ENV" | cut -d= -f2-)
ESTIMATED_TOKENS=$(grep -m1 '^ESTIMATED_TOKENS=' "$TOKENS_ENV" | cut -d= -f2-)

small_tokens=$(jq -r '.thresholds.small_tokens' "$CONFIG")
medium_tokens=$(jq -r '.thresholds.medium_tokens' "$CONFIG")
max_tokens=$(jq -r '.thresholds.max_tokens' "$CONFIG")
if [[ -n "${MAX_ESTIMATED_TOKENS:-}" ]]; then
  max_tokens="$MAX_ESTIMATED_TOKENS"
fi

safety_factor=$(jq -r '.ram_estimate.safety_factor // 1.4' "$CONFIG")
base_overhead_gb=$(jq -r '.ram_estimate.base_overhead_gb // 2' "$CONFIG")

mem_avail_kb=$(awk '/MemAvailable:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)
free_ram_gb=$(( mem_avail_kb / 1024 / 1024 ))

# Exact parameter count from the HF API; falls back to parsing "<n>B" from the
# model id (e.g. Qwen2.5-1.5B -> 1.5e9) when the API is unreachable.
model_param_count() {
  local id="$1" total=""
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
  local id="$1" quant="$2" params bytes
  params=$(model_param_count "$id")
  bytes=$(dtype_bytes "$quant")
  awk -v p="$params" -v b="$bytes" -v sf="$safety_factor" -v ov="$base_overhead_gb" \
    'BEGIN { r = (p * b / 1073741824) * sf + ov; printf "%d", (r == int(r)) ? r : int(r) + 1 }'
}

SKIP_REVIEW=false
REASON=""
REQUIRED_RAM_GB=0

if [[ "${ESTIMATED_TOKENS:-0}" -gt "$max_tokens" ]]; then
  SKIP_REVIEW=true
  REASON="Estimated tokens (${ESTIMATED_TOKENS}) exceed max (${max_tokens})"
fi

# Token estimate picks the starting tier (and the RAM-fit fallback order).
if [[ "${ESTIMATED_TOKENS:-0}" -lt "$small_tokens" ]]; then
  tier=small
  candidates=(small)
elif [[ "${ESTIMATED_TOKENS:-0}" -le "$medium_tokens" ]]; then
  tier=medium
  candidates=(medium small)
else
  tier=large_load
  candidates=(large_load medium small)
fi

if [[ "$EXTERNAL_LLM" == "true" ]]; then
  # External API: no local weights, so no RAM estimate or fit check. Use the
  # override as the model name if given, else the tier default; concurrency
  # still follows the token tier.
  if [[ -n "$MODEL_OVERRIDE" ]]; then
    MAX_MODEL="$MODEL_OVERRIDE"
  else
    MAX_MODEL=$(jq -r ".models.${tier}.id" "$CONFIG")
  fi
  MAX_QUANT="${MODEL_OVERRIDE_QUANT:-}"
  OCR_CONCURRENCY=$(jq -r ".models.${tier}.concurrency" "$CONFIG")
elif [[ -n "$MODEL_OVERRIDE" ]]; then
  MAX_MODEL="$MODEL_OVERRIDE"
  MAX_QUANT="${MODEL_OVERRIDE_QUANT:-float32}"
  OCR_CONCURRENCY=3
  REQUIRED_RAM_GB=$(required_ram_gib "$MAX_MODEL" "$MAX_QUANT")
  if [[ "$free_ram_gb" -lt "$REQUIRED_RAM_GB" ]]; then
    REASON="Override ${MAX_MODEL} (${MAX_QUANT}) needs ~${REQUIRED_RAM_GB}GB but only ${free_ram_gb}GB free; may OOM"
    echo "WARNING: $REASON" >&2
  fi
else
  # Local MAX: RAM fit downgrades through the candidate tiers.
  CHOSEN=""
  for cand in "${candidates[@]}"; do
    cid=$(jq -r ".models.${cand}.id" "$CONFIG")
    cq=$(jq -r ".models.${cand}.quantization" "$CONFIG")
    need=$(required_ram_gib "$cid" "$cq")
    if [[ "$free_ram_gb" -ge "$need" ]]; then
      MAX_MODEL="$cid"
      MAX_QUANT="$cq"
      OCR_CONCURRENCY=$(jq -r ".models.${cand}.concurrency" "$CONFIG")
      REQUIRED_RAM_GB="$need"
      CHOSEN="$cand"
      break
    fi
    echo "Tier ${cand} (${cid}, ${cq}) needs ~${need}GB, only ${free_ram_gb}GB free; trying smaller." >&2
    SMALLEST_ID="$cid"; SMALLEST_QUANT="$cq"; SMALLEST_NEED="$need"
  done

  if [[ -z "$CHOSEN" ]]; then
    # Even the smallest candidate does not fit: surface it and skip the review.
    MAX_MODEL="$SMALLEST_ID"
    MAX_QUANT="$SMALLEST_QUANT"
    OCR_CONCURRENCY=2
    REQUIRED_RAM_GB="$SMALLEST_NEED"
    SKIP_REVIEW=true
    REASON="No model fits: smallest (${MAX_MODEL}) needs ~${SMALLEST_NEED}GB but only ${free_ram_gb}GB free"
  fi
fi

max_bundles=$(jq -r '.thresholds.max_bundles_before_concurrency_cap' "$CONFIG")
if [[ "${BUNDLE_COUNT:-0}" -gt "$max_bundles" ]]; then
  OCR_CONCURRENCY=$(( OCR_CONCURRENCY > 2 ? 2 : OCR_CONCURRENCY ))
fi

{
  echo "ESTIMATED_TOKENS=${ESTIMATED_TOKENS}"
  echo "MAX_MODEL=${MAX_MODEL}"
  echo "MAX_QUANT=${MAX_QUANT}"
  echo "OCR_CONCURRENCY=${OCR_CONCURRENCY}"
  echo "FREE_RAM_GB=${free_ram_gb}"
  echo "REQUIRED_RAM_GB=${REQUIRED_RAM_GB}"
  echo "SKIP_REVIEW=${SKIP_REVIEW}"
  echo "SKIP_REASON=${REASON}"
} | tee /tmp/ocr-model.env

echo "Selected model: ${MAX_MODEL} (quant=${MAX_QUANT}, concurrency=${OCR_CONCURRENCY}, need~${REQUIRED_RAM_GB}GB, free=${free_ram_gb}GB)"
