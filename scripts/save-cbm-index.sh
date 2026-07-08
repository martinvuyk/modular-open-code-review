#!/usr/bin/env bash
# Export codebase-memory-mcp index artifacts for actions/cache.
set -euo pipefail

CACHE_ROOT="${1:-${RUNNER_TOOL_CACHE:-/tmp}/cbm-index}"
CBM_CACHE_DIR="${CBM_CACHE_DIR:-${RUNNER_TOOL_CACHE:-/tmp}/codebase-memory-mcp}"

mkdir -p "$CACHE_ROOT"

if [[ -f "${GITHUB_WORKSPACE}/.codebase-memory/graph.db.zst" ]]; then
  cp "${GITHUB_WORKSPACE}/.codebase-memory/graph.db.zst" "${CACHE_ROOT}/graph.db.zst"
  echo "Exported .codebase-memory/graph.db.zst"
elif [[ -d "$CBM_CACHE_DIR" ]] && [[ -n "$(ls -A "$CBM_CACHE_DIR" 2>/dev/null || true)" ]]; then
  tar -czf "${CACHE_ROOT}/cbm-cache.tar.gz" -C "$(dirname "$CBM_CACHE_DIR")" "$(basename "$CBM_CACHE_DIR")"
  echo "Exported ${CBM_CACHE_DIR} as tarball"
else
  echo "Nothing to export from CBM cache." >&2
  exit 1
fi
