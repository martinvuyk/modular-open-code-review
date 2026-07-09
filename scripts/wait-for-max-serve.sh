#!/usr/bin/env bash
# Wait for max serve to become healthy, but fail fast when the process dies.
#
# Without the pid check, a crashed serve (e.g. unsupported quantization) leaves
# nothing listening on the port and we burn the full SERVE_TIMEOUT (30 min).
#
# Usage: wait-for-max-serve.sh <url> <pid> <log_file> [timeout_seconds] [interval]
set -euo pipefail

URL="${1:?usage: wait-for-max-serve.sh <url> <pid> <log_file> [timeout] [interval]}"
PID="${2:?missing pid}"
LOG="${3:?missing log file}"
TIMEOUT="${4:-1800}"
INTERVAL="${5:-5}"

deadline=$((SECONDS + TIMEOUT))

echo "Waiting for ${URL} (pid ${PID}, timeout ${TIMEOUT}s)..."
while (( SECONDS < deadline )); do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "max serve exited before ${URL} became ready (pid ${PID})." >&2
    if [[ -f "$LOG" ]]; then
      echo "---- tail ${LOG} ----" >&2
      tail -n 80 "$LOG" >&2 || true
    fi
    exit 1
  fi
  if curl -fsS "$URL" >/dev/null 2>&1; then
    echo "Endpoint ready: ${URL}"
    exit 0
  fi
  sleep "$INTERVAL"
done

echo "Timed out waiting for ${URL}" >&2
if [[ -f "$LOG" ]]; then
  echo "---- tail ${LOG} ----" >&2
  tail -n 80 "$LOG" >&2 || true
fi
exit 1
