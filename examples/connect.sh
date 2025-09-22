#!/usr/bin/env bash
set -euo pipefail

# Client-side helper: prefer mosh, fall back to ssh.
# Usage: examples/connect.sh user@host [ssh/mosh args...]

if [ "$#" -lt 1 ]; then
  echo "Usage: $(basename "$0") user@host [args...]" >&2
  exit 2
fi

host="$1"
shift || true

if command -v mosh >/dev/null; then
  echo "Connecting with mosh ... (will fall back to ssh if it fails)" >&2
  if mosh -- "$host" -- "$@"; then
    exit 0
  else
    echo "mosh failed; falling back to ssh" >&2
  fi
fi

exec ssh "$host" "$@"

