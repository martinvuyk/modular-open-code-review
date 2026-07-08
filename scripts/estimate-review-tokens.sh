#!/usr/bin/env bash
# Estimate PR review token usage from OCR preview JSON and git diff stats.
set -euo pipefail

PREVIEW_JSON="${1:-/tmp/ocr-preview.json}"
BASE_REF="${2:?usage: estimate-review-tokens.sh <preview.json> <base_ref> <head_ref>}"
HEAD_REF="${3:?usage: estimate-review-tokens.sh <preview.json> <base_ref> <head_ref>}"
CONFIG="${4:-${GITHUB_ACTION_PATH:-.}/../config/models.cpu.json}"

if [[ ! -f "$CONFIG" ]]; then
  CONFIG="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/config/models.cpu.json"
fi

bytes_per_token=$(jq -r '.heuristics.bytes_per_token' "$CONFIG")
bundle_overhead=$(jq -r '.heuristics.prompt_overhead_per_bundle' "$CONFIG")
file_overhead=$(jq -r '.heuristics.context_overhead_per_file' "$CONFIG")
mcp_buffer=$(jq -r '.heuristics.mcp_tool_buffer' "$CONFIG")

bundle_count=$(jq '
  if (.bundles | type) == "array" then (.bundles | length)
  elif (.preview.bundles | type) == "array" then (.preview.bundles | length)
  else 0 end
' "$PREVIEW_JSON" 2>/dev/null || echo 0)

file_count=$(jq '
  if (.files | type) == "array" then (.files | length)
  elif (.preview.files | type) == "array" then (.preview.files | length)
  elif (.bundles | type) == "array" then ([.bundles[].files? // .bundles[]? | select(type=="string")] | length)
  else 0 end
' "$PREVIEW_JSON" 2>/dev/null || echo 0)

if [[ "$file_count" == "0" ]]; then
  file_count=$(jq '[.. | strings | select(test("^[^/]") or test("^src/") or test("^\\.?/?[a-zA-Z]"))] | length' "$PREVIEW_JSON" 2>/dev/null || echo 0)
fi

if [[ "$bundle_count" == "0" && "$file_count" -gt 0 ]]; then
  bundle_count=$(( (file_count + 2) / 3 ))
fi

changed_bytes=0
changed_lines=0
if git rev-parse --verify "${BASE_REF}^{commit}" >/dev/null 2>&1 \
  && git rev-parse --verify "${HEAD_REF}^{commit}" >/dev/null 2>&1; then
  while IFS=$'\t' read -r add del _; do
    [[ -z "${add:-}" || "$add" == "-" ]] && continue
    [[ -z "${del:-}" || "$del" == "-" ]] && continue
    changed_lines=$((changed_lines + add + del))
  done < <(git diff --numstat "${BASE_REF}" "${HEAD_REF}" 2>/dev/null || true)

  changed_bytes=$(git diff "${BASE_REF}" "${HEAD_REF}" 2>/dev/null | wc -c | tr -d ' ')
fi

diff_tokens=$(( changed_bytes / bytes_per_token ))
bundle_tokens=$(( bundle_count * bundle_overhead ))
file_tokens=$(( file_count * file_overhead ))
estimated_tokens=$(( diff_tokens + bundle_tokens + file_tokens + mcp_buffer ))

{
  echo "BUNDLE_COUNT=${bundle_count}"
  echo "FILE_COUNT=${file_count}"
  echo "CHANGED_LINES=${changed_lines}"
  echo "CHANGED_BYTES=${changed_bytes}"
  echo "ESTIMATED_TOKENS=${estimated_tokens}"
} | tee /tmp/ocr-tokens.env

echo "Estimated review tokens: ${estimated_tokens} (${bundle_count} bundles, ${file_count} files, ${changed_lines} changed lines)"
