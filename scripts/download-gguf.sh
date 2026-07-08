#!/usr/bin/env bash
# Download a GGUF weight file (and any sibling shards) from the Hugging Face Hub
# and print the local path of the primary file to stdout.
#
# MAX's --weight-path wants a local file, so we fetch it up front. If the file
# is sharded (…-00001-of-000NN.gguf), every shard is downloaded into the same
# directory and the first shard's path is printed (MAX/gguf loaders discover the
# rest by naming convention).
#
# Usage: download-gguf.sh <repo_id> <filename> <dest_dir>
# All human-readable logging goes to stderr; stdout is the path only.
set -euo pipefail

REPO="${1:?usage: download-gguf.sh <repo_id> <filename> <dest_dir>}"
FILE="${2:?missing filename}"
DEST="${3:?missing dest_dir}"

mkdir -p "$DEST"

# Collect the set of files to fetch. For a sharded primary, expand to all shards.
files=("$FILE")
if [[ "$FILE" =~ ^(.*)-([0-9]{5})-of-([0-9]{5})\.gguf$ ]]; then
  prefix="${BASH_REMATCH[1]}"
  total="${BASH_REMATCH[3]}"
  files=()
  for (( n = 1; n <= 10#$total; n++ )); do
    files+=("$(printf '%s-%05d-of-%s.gguf' "$prefix" "$n" "$total")")
  done
  echo "download-gguf: ${FILE} is shard 1 of ${total}; fetching all shards." >&2
fi

primary=""
for f in "${files[@]}"; do
  echo "download-gguf: fetching ${REPO}/${f}" >&2
  path=$(REPO="$REPO" FNAME="$f" DEST="$DEST" python - >/dev/null 2>/tmp/gguf-dl.err <<'PY' && cat /tmp/gguf-dl.path
import os, subprocess, sys
try:
    from huggingface_hub import hf_hub_download
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "huggingface_hub"])
    from huggingface_hub import hf_hub_download
p = hf_hub_download(
    repo_id=os.environ["REPO"],
    filename=os.environ["FNAME"],
    local_dir=os.environ["DEST"],
    token=os.environ.get("HF_TOKEN") or None,
)
open("/tmp/gguf-dl.path", "w").write(p)
PY
) || { echo "download-gguf: FAILED for ${REPO}/${f}" >&2; cat /tmp/gguf-dl.err >&2 || true; exit 1; }
  [[ -z "$primary" ]] && primary="$path"
done

printf '%s' "$primary"
