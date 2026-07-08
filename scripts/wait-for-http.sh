#!/usr/bin/env bash
# Poll an HTTP endpoint until it returns success or timeout.
set -euo pipefail

URL="${1:?usage: wait-for-http.sh <url> [timeout_seconds]}"
TIMEOUT="${2:-600}"
INTERVAL="${3:-5}"

deadline=$((SECONDS + TIMEOUT))

echo "Waiting for ${URL} (timeout ${TIMEOUT}s)..."
while (( SECONDS < deadline )); do
  if curl -k -fsS "$URL" >/dev/null 2>&1; then
    echo "Endpoint ready: ${URL}"
    exit 0
  fi
  sleep "$INTERVAL"
done

echo "Timed out waiting for ${URL}" >&2
exit 1
