#!/usr/bin/env bash
# Shared max warm-cache / max serve args for a Hugging Face model ID.
# GGUF repos need --quantization-encoding; safetensors models must not pass it.
set -euo pipefail

MODEL="${1:?usage: max-model-cli.sh <model_id> [quantization]}"
QUANT="${2:-}"

printf '%s\0' --devices=cpu --model "$MODEL"
if [[ -n "$QUANT" ]]; then
  printf '%s\0' --quantization-encoding "$QUANT"
fi
