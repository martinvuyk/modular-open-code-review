#!/usr/bin/env bash
# Shared max warm-cache / max serve args for a Hugging Face model ID.
# On CPU, safetensors models require --quantization-encoding float32 (bfloat16 is GPU-only).
set -euo pipefail

MODEL="${1:?usage: max-model-cli.sh <model_id> [quantization]}"
QUANT="${2:-float32}"

printf '%s\0' --devices=cpu --model "$MODEL"
printf '%s\0' --quantization-encoding "$QUANT"
