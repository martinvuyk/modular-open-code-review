#!/usr/bin/env bash
# OCR may write telemetry JSON lines to stdout before the final review result when
# OCR_ENABLE_TELEMETRY=1. GitHub Actions captures all of stdout into one file.
# Keep the last JSON object that contains a .summary (the real review result).
set -euo pipefail

src="${1:?usage: normalize-ocr-result.sh <raw_stdout> <clean_result>}"
dst="${2:?missing output path}"

if [[ ! -s "$src" ]]; then
  : > "$dst"
  exit 0
fi

result=""
# JSONL or multiple JSON values on stdout (-s slurps into an array).
if result=$(jq -cs '[.[] | select(type == "object" and .summary != null)] | last // empty' "$src" 2>/dev/null); then
  :
fi

if [[ -z "$result" || "$result" == "null" ]]; then
  # Single JSON object file.
  result=$(jq -c 'select(.summary != null)' "$src" 2>/dev/null | tail -n 1 || true)
fi

if [[ -z "$result" || "$result" == "null" ]]; then
  echo "WARNING: no OCR result object with .summary found; keeping raw stdout." >&2
  cp "$src" "$dst"
else
  printf '%s\n' "$result" > "$dst"
fi
