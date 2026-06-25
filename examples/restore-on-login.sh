#!/usr/bin/env bash
# Example: source this from ~/.bashrc or ~/.zshrc to auto-restore on login.
# It restores only when tmux is available, the session is absent, and the
# configured manifest exists.

codexfarm_restore_on_login() {
  local session manifest restore_bin
  session="${CODEX_SESSION:-codexfarm}"
  manifest="${CODEX_RESTORE_MANIFEST:-${XDG_CONFIG_HOME:-$HOME/.config}/codexfarm/manifest.tsv}"
  restore_bin="${CODEX_RESTORE_BIN:-codex-restore}"

  command -v tmux >/dev/null 2>&1 || return 0
  tmux has-session -t "$session" >/dev/null 2>&1 && return 0
  [ -f "$manifest" ] || return 0

  if ! command -v "$restore_bin" >/dev/null 2>&1; then
    echo "[codex-farm] restore command not found: $restore_bin" >&2
    return 127
  fi

  echo "[codex-farm] Restoring from manifest ..."
  "$restore_bin" "$manifest"
}

codexfarm_restore_on_login
