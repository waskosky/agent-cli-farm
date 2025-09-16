#!/usr/bin/env bash
set -euo pipefail

# Batch-add multiple projects into the Codex farm without attaching.
# Usage:
#   examples/batch-add.sh dir1 dir2 ...
# Env:
#   CODEX_CMD, CODEX_ARGS, CODEX_SESSION are respected by codex-add.

if [ "$#" -lt 1 ]; then
  echo "Usage: $(basename "$0") DIR [DIR ...]" >&2
  exit 2
fi

for d in "$@"; do
  if [ -d "$d" ]; then
    name=$(basename "$(readlink -f "$d")")
    echo "Adding $name ($d) ..."
    CODEX_NAME="$name" codex-add -d "$d"
  else
    echo "Skipping non-directory: $d" >&2
  fi
done

echo "Batch add complete. Attach with: tmux attach -t ${CODEX_SESSION:-codexfarm}"

