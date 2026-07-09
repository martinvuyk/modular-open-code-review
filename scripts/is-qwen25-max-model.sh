#!/usr/bin/env bash
# True when a Hugging Face model ID is Qwen 2.5 (local MAX tool-call proxy target).
#
# Usage:
#   source scripts/is-qwen25-max-model.sh
#   is_qwen25_max_model "Qwen/Qwen2.5-1.5B-Instruct" && echo yes
#
#   scripts/is-qwen25-max-model.sh "Qwen/Qwen2.5-1.5B-Instruct"
#   # exit 0 = Qwen 2.5, exit 1 = not
set -euo pipefail

is_qwen25_max_model() {
  local model_id="${1:-}"
  [[ -n "$model_id" ]] || return 1
  local lower="${model_id,,}"
  [[ "$lower" =~ qwen2\.5 ]]
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  is_qwen25_max_model "${1:?usage: is-qwen25-max-model.sh <model_id>}"
fi
