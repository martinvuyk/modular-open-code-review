#!/usr/bin/env bash
# Update consumer-facing @ref pins in README, examples, and config/release.json.
set -euo pipefail

TAG="${1:?usage: bump-consumer-refs.sh <tag>  (e.g. v1.0.0 or 1.0.0)}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "$TAG" != v* ]]; then
  REF="v${TAG}"
else
  REF="${TAG}"
fi

echo "Bumping consumer refs to @${REF}"

for file in "${ROOT}/README.md" "${ROOT}/examples/"*.yml; do
  [[ -f "$file" ]] || continue
  sed -i -E "s#(martinvuyk/modular-open-code-review/[^@\"']+)@[^ \"']+#\\1@${REF}#g" "$file"
done

RELEASE_JSON="${ROOT}/config/release.json"
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
jq -n --arg ref "$REF" --arg updated "$TIMESTAMP" \
  '{consumer_ref: $ref, updated_at: $updated}' > "$RELEASE_JSON"

echo "Updated consumer_ref to ${REF}"
