#!/usr/bin/env bash
# Select MAX model and OCR concurrency from token estimate and available RAM.
set -euo pipefail

TOKENS_ENV="${1:-/tmp/ocr-tokens.env}"
CONFIG="${2:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/config/models.cpu.json}"
MODEL_OVERRIDE="${MODEL_OVERRIDE:-}"

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
min_ram_gb=$(jq -r '.thresholds.min_free_ram_gb_for_7b' "$CONFIG")

mem_avail_kb=$(awk '/MemAvailable:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)
free_ram_gb=$(( mem_avail_kb / 1024 / 1024 ))

SKIP_REVIEW=false
REASON=""

if [[ "${ESTIMATED_TOKENS:-0}" -gt "$max_tokens" ]]; then
  SKIP_REVIEW=true
  REASON="Estimated tokens (${ESTIMATED_TOKENS}) exceed max (${max_tokens})"
fi

if [[ -n "$MODEL_OVERRIDE" ]]; then
  MAX_MODEL="$MODEL_OVERRIDE"
  MAX_QUANT="q4_k"
  OCR_CONCURRENCY=3
else
  if [[ "${ESTIMATED_TOKENS:-0}" -lt "$small_tokens" ]]; then
    MAX_MODEL=$(jq -r '.models.small.id' "$CONFIG")
    MAX_QUANT=$(jq -r '.models.small.quantization' "$CONFIG")
    OCR_CONCURRENCY=$(jq -r '.models.small.concurrency' "$CONFIG")
  elif [[ "${ESTIMATED_TOKENS:-0}" -le "$medium_tokens" ]]; then
    if [[ "$free_ram_gb" -ge "$min_ram_gb" ]]; then
      MAX_MODEL=$(jq -r '.models.medium.id' "$CONFIG")
      MAX_QUANT=$(jq -r '.models.medium.quantization' "$CONFIG")
      OCR_CONCURRENCY=$(jq -r '.models.medium.concurrency' "$CONFIG")
    else
      MAX_MODEL=$(jq -r '.models.small.id' "$CONFIG")
      MAX_QUANT=$(jq -r '.models.small.quantization' "$CONFIG")
      OCR_CONCURRENCY=2
    fi
  else
    MAX_MODEL=$(jq -r '.models.large_load.id' "$CONFIG")
    MAX_QUANT=$(jq -r '.models.large_load.quantization' "$CONFIG")
    OCR_CONCURRENCY=$(jq -r '.models.large_load.concurrency' "$CONFIG")
  fi
fi

max_bundles=$(jq -r '.thresholds.max_bundles_before_concurrency_cap' "$CONFIG")
if [[ "${BUNDLE_COUNT:-0}" -gt "$max_bundles" ]]; then
  OCR_CONCURRENCY=$(( OCR_CONCURRENCY > 2 ? 2 : OCR_CONCURRENCY ))
fi

{
  echo "MAX_MODEL=${MAX_MODEL}"
  echo "MAX_QUANT=${MAX_QUANT}"
  echo "OCR_CONCURRENCY=${OCR_CONCURRENCY}"
  echo "FREE_RAM_GB=${free_ram_gb}"
  echo "SKIP_REVIEW=${SKIP_REVIEW}"
  echo "SKIP_REASON=${REASON}"
} | tee /tmp/ocr-model.env

echo "Selected model: ${MAX_MODEL} (quant=${MAX_QUANT}, concurrency=${OCR_CONCURRENCY}, free_ram=${free_ram_gb}GB)"
