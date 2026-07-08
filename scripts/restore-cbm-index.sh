#!/usr/bin/env bash
# Restore a cached codebase-memory-mcp index into CBM_CACHE_DIR.
set -euo pipefail

CACHE_ROOT="${1:-${RUNNER_TOOL_CACHE:-/tmp}/cbm-index}"
CBM_CACHE_DIR="${CBM_CACHE_DIR:-${RUNNER_TOOL_CACHE:-/tmp}/codebase-memory-mcp}"

mkdir -p "$CBM_CACHE_DIR"

if [[ -f "${CACHE_ROOT}/graph.db.zst" ]]; then
  echo "Restoring graph.db.zst into ${CBM_CACHE_DIR}..."
  mkdir -p "${GITHUB_WORKSPACE}/.codebase-memory"
  cp "${CACHE_ROOT}/graph.db.zst" "${GITHUB_WORKSPACE}/.codebase-memory/graph.db.zst"
elif [[ -f "${CACHE_ROOT}/cbm-cache.tar.gz" ]]; then
  echo "Restoring cbm-cache.tar.gz into ${CBM_CACHE_DIR}..."
  tar -xzf "${CACHE_ROOT}/cbm-cache.tar.gz" -C "$(dirname "$CBM_CACHE_DIR")"
else
  echo "No cached index found at ${CACHE_ROOT}; starting cold."
fi

export CBM_CACHE_DIR
echo "CBM_CACHE_DIR=${CBM_CACHE_DIR}"
