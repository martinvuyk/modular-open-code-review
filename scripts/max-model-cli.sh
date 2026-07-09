#!/usr/bin/env bash
# Shared `max warm-cache` / `max serve` args for a model.
#
# Safetensors models run on CPU with --quantization-encoding float32 (bfloat16
# is GPU-only). GGUF models pass a quantized encoding (q4_k/q6_k) plus a local
# --weight-path; --model then only provides the architecture/config/tokenizer.
# Args are NUL-delimited so paths with spaces survive `mapfile -d ''`.
set -euo pipefail

MODEL="${1:?usage: max-model-cli.sh <model_id> [quantization] [weight_path] [tool_parser] [reasoning_parser]}"
QUANT="${2:-float32}"
WEIGHT_PATH="${3:-}"
TOOL_PARSER="${4:-}"
REASONING_PARSER="${5:-}"

printf '%s\0' --devices=cpu --model "$MODEL"
printf '%s\0' --quantization-encoding "$QUANT"
if [[ -n "$WEIGHT_PATH" ]]; then
  printf '%s\0' --weight-path "$WEIGHT_PATH"
fi
if [[ -n "$TOOL_PARSER" ]]; then
  printf '%s\0' --tool-parser "$TOOL_PARSER"
fi
if [[ -n "$REASONING_PARSER" ]]; then
  printf '%s\0' --reasoning-parser "$REASONING_PARSER"
fi
