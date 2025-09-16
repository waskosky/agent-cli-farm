#!/usr/bin/env bash
# Example: add this to your ~/.bashrc or ~/.zshrc to auto-restore on login
# It restores only if the session is not already running.

set -euo pipefail

SESSION="${CODEX_SESSION:-codexfarm}"
MANIFEST="${XDG_CONFIG_HOME:-$HOME/.config}/codexfarm/manifest.tsv"

if command -v tmux >/dev/null; then
  if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    if [ -f "$MANIFEST" ]; then
      echo "[codex-farm] Restoring from manifest ..."
      codex-restore "$MANIFEST" || true
    fi
  fi
fi

