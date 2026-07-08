#!/usr/bin/env bash
# Pre-download and compile MAX models (populates HF + MODULAR_MAX_CACHE_DIR).
set -euo pipefail

CONFIG="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/config/models.cpu.json}"
MODULAR_VERSION="${MODULAR_VERSION:-26.4.0}"
DEVICES="${MAX_DEVICES:-cpu}"

if [[ ! -f "$CONFIG" ]]; then
  echo "Missing config: $CONFIG" >&2
  exit 1
fi

mapfile -t MODEL_SPECS < <(jq -r '
  [.models[] | {id: .id, quant: .quantization}] |
  unique_by(.id) |
  .[] | "\(.id)\t\(.quant)"
' "$CONFIG")

if [[ "${#MODEL_SPECS[@]}" -eq 0 ]]; then
  echo "No models found in $CONFIG" >&2
  exit 1
fi

export HF_HOME="${HF_HOME:-${HOME}/.cache/huggingface}"
export MODULAR_MAX_CACHE_DIR="${MODULAR_MAX_CACHE_DIR:-${HOME}/.cache/modular-max}"

if ! command -v max >/dev/null 2>&1; then
  echo "Installing modular==${MODULAR_VERSION}..."
  python -m pip install --upgrade pip
  pip install "modular==${MODULAR_VERSION}"
fi

max --version

for spec in "${MODEL_SPECS[@]}"; do
  model="${spec%%$'\t'*}"
  quant="${spec#*$'\t'}"
  echo "Warming MAX cache for ${model} (quant=${quant}, devices=${DEVICES})..."
  max warm-cache \
    --devices="$DEVICES" \
    --model "$model" \
    --quantization-encoding "$quant"
done

echo "MAX model cache warm complete."
