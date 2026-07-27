#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
tmp_root="$(mktemp -d)"

cleanup() {
  set +e
  if command -v tmux >/dev/null 2>&1; then
    tmux kill-server >/dev/null 2>&1 || true
  fi
  rm -rf "$tmp_root"
}
trap cleanup EXIT HUP INT TERM

home_dir="$tmp_root/home"
config_dir="$tmp_root/config"
state_dir="$tmp_root/state"
private_bin="$tmp_root/bin"
project_dir="$tmp_root/project"
tmux_tmp="$tmp_root/tmux"
ready_dir="$tmp_root/ready"
mkdir -p \
  "$home_dir" "$config_dir" "$state_dir" "$private_bin" "$project_dir" \
  "$tmux_tmp" "$ready_dir"
chmod 700 "$tmux_tmp"

export HOME="$home_dir"
export XDG_CONFIG_HOME="$config_dir"
export XDG_STATE_HOME="$state_dir"
export TMUX_TMPDIR="$tmux_tmp"
export PATH="$private_bin:$repo_root/bin:$PATH"
export CODEX_ANNOTATOR_AUTOSTART=0
export CODEX_AUTOSERVICE_CHOICE=no
export CODEXFARM_HISTORY_BACKEND=legacy
export CODEX_LOCK_TITLES=1
export CODEX_REMAIN_ON_EXIT=0
export CODEX_TIPS_PROMPT=0
unset TMUX

session="codexfarm-resume-smoke-$$"
manifest="$config_dir/codexfarm/session-resume.tsv"
invocation_log="$tmp_root/provider-invocations.log"
session_map="$tmp_root/session-map.tsv"

codex_a="019e1659-3a2f-7a40-95cf-5ac9dd7fe5d4"
codex_b="123e4567-e89b-42d3-a456-426614174001"
claude_a="54f5b65c-a31c-4aa1-b91b-896b35e2a759"
claude_b="123e4567-e89b-42d3-a456-426614174002"
gemini_a="27bd36d0-2977-4cce-9d5d-33764d915f1d"
gemini_b="123e4567-e89b-42d3-a456-426614174003"

codex_a_file="$HOME/.codex/sessions/2026/07/15/rollout-$codex_a.jsonl"
codex_b_file="$HOME/.codex/sessions/2026/07/16/rollout-$codex_b.jsonl"
claude_a_file="$HOME/.claude/projects/-tmp-project/$claude_a.jsonl"
claude_b_file="$HOME/.claude/projects/-tmp-project/$claude_b.jsonl"
gemini_a_file="$HOME/.gemini/tmp/project-hash/chats/session-a.jsonl"
gemini_b_file="$HOME/.gemini/tmp/project-hash/chats/session-b.jsonl"

mkdir -p \
  "$(dirname "$codex_a_file")" "$(dirname "$codex_b_file")" \
  "$(dirname "$claude_a_file")" "$(dirname "$claude_b_file")" \
  "$(dirname "$gemini_a_file")"
printf '{"type":"session_meta","payload":{"id":"%s"}}\n' "$codex_a" > "$codex_a_file"
printf '{"type":"session_meta","payload":{"id":"%s"}}\n' "$codex_b" > "$codex_b_file"
printf '{"sessionId":"%s"}\n' "$claude_a" > "$claude_a_file"
printf '{"sessionId":"%s"}\n' "$claude_b" > "$claude_b_file"
printf '{"sessionId":"%s","projectHash":"project-hash"}\n' \
  "$gemini_a" > "$gemini_a_file"
printf '{"sessionId":"%s","projectHash":"project-hash"}\n' \
  "$gemini_b" > "$gemini_b_file"

{
  printf 'codex-a\t%s\n' "$codex_a_file"
  printf 'codex-b\t%s\n' "$codex_b_file"
  printf 'claude-a\t%s\n' "$claude_a_file"
  printf 'claude-b\t%s\n' "$claude_b_file"
  printf 'gemini-a\t%s\n' "$gemini_a_file"
  printf 'gemini-b\t%s\n' "$gemini_b_file"
} > "$session_map"

for provider in codex claude gemini; do
  provider_bin="$private_bin/$provider"
  cp /dev/null "$provider_bin"
  chmod +x "$provider_bin"
done

mock_provider='#!/usr/bin/env bash
set -euo pipefail
provider="${0##*/}"
printf "%s|%s\n" "$provider" "$*" >> "$MOCK_PROVIDER_INVOCATION_LOG"
label="${1:-}"
session_file=""
while IFS=$'"'"'\t'"'"' read -r candidate path; do
  if [ "$candidate" = "$label" ]; then
    session_file="$path"
    break
  fi
done < "$MOCK_PROVIDER_SESSION_MAP"
if [ -n "$session_file" ]; then
  exec 9< "$session_file"
  ready_key="$label"
else
  last_arg=""
  for value in "$@"; do
    last_arg="$value"
  done
  ready_key="$provider-$last_arg"
fi
: > "$MOCK_PROVIDER_READY_DIR/$ready_key"
trap "exit 0" TERM INT
while :; do
  sleep 1
done
'
for provider in codex claude gemini; do
  printf '%s' "$mock_provider" > "$private_bin/$provider"
done

export MOCK_PROVIDER_INVOCATION_LOG="$invocation_log"
export MOCK_PROVIDER_SESSION_MAP="$session_map"
export MOCK_PROVIDER_READY_DIR="$ready_dir"

wait_for_file() {
  local path="$1"
  local attempt=0
  while [ "$attempt" -lt 100 ]; do
    [ -e "$path" ] && return 0
    sleep 0.05
    attempt=$((attempt + 1))
  done
  echo "Timed out waiting for provider readiness" >&2
  return 1
}

require_manifest_row() {
  local name="$1"
  local command="$2"
  local args="$3"
  if ! awk -F '\t' -v name="$name" -v command="$command" -v args="$args" '
    $1 == name && $3 == command && $4 == args { found = 1 }
    END { exit(found ? 0 : 1) }
  ' "$manifest"; then
    echo "Missing exact manifest row for $name" >&2
    return 1
  fi
}

require_invocation() {
  local expected="$1"
  local attempt=0
  while [ "$attempt" -lt 100 ]; do
    if [ -f "$invocation_log" ] && grep -Fqx "$expected" "$invocation_log"; then
      return 0
    fi
    sleep 0.05
    attempt=$((attempt + 1))
  done
  echo "Timed out waiting for exact restored provider invocation" >&2
  return 1
}

start_provider() {
  local provider="$1"
  local label="$2"
  CODEX_SESSION="$session" \
    CODEX_NAME="$label" \
    CODEX_CMD="$private_bin/$provider" \
    "$repo_root/bin/$provider-add" -d "$project_dir" -- "$label" >/dev/null
}

echo "=== Exact Multi-Session Resume Integration ==="

for provider in codex claude gemini; do
  start_provider "$provider" "$provider-a"
  start_provider "$provider" "$provider-b"
done
for label in codex-a codex-b claude-a claude-b gemini-a gemini-b; do
  wait_for_file "$ready_dir/$label"
done

CODEX_SESSION="$session" "$repo_root/bin/codex-save" "$manifest" >/dev/null
exact_provider_rows="$(
  awk -F '\t' '
    $3 == "codex" && $4 ~ /^resume [0-9a-f-]+$/ { exact++ }
    $3 == "claude" && $4 ~ /^--resume [0-9a-f-]+$/ { exact++ }
    $3 == "gemini" && $4 ~ /^--resume [0-9a-f-]+$/ { exact++ }
    END { print exact + 0 }
  ' "$manifest"
)"
if [ "$exact_provider_rows" -ne 6 ]; then
  echo "Expected six exact provider rows, found $exact_provider_rows" >&2
  exit 1
fi

require_manifest_row codex-a codex "resume $codex_a"
require_manifest_row codex-b codex "resume $codex_b"
require_manifest_row claude-a claude "--resume $claude_a"
require_manifest_row claude-b claude "--resume $claude_b"
require_manifest_row gemini-a gemini "--resume $gemini_a"
require_manifest_row gemini-b gemini "--resume $gemini_b"
if grep -Eq $'\t(codex\tresume --last|claude\t--continue|gemini\t--resume latest)$' \
  "$manifest"
then
  echo "Save unexpectedly used a provider fallback" >&2
  exit 1
fi
echo "[OK] save kept two exact sessions distinct for every provider"

for label in codex-a codex-b claude-a claude-b gemini-a gemini-b; do
  stable_name="$(
    tmux show-options -p -v -t "$session:$label.0" @codexfarm_name
  )"
  [ "$stable_name" = "$label" ]
done
echo "[OK] managed panes retained stable logical names"

tmux kill-session -t "$session"
: > "$invocation_log"
find "$ready_dir" -type f -delete

CODEX_SESSION="$session" "$repo_root/bin/codex-restore" "$manifest" >/dev/null
for provider_and_id in \
  "codex:$codex_a" "codex:$codex_b" \
  "claude:$claude_a" "claude:$claude_b" \
  "gemini:$gemini_a" "gemini:$gemini_b"
do
  provider="${provider_and_id%%:*}"
  session_id="${provider_and_id#*:}"
  wait_for_file "$ready_dir/$provider-$session_id"
  case "$provider" in
    codex) require_invocation "codex|resume $session_id" ;;
    claude) require_invocation "claude|--resume $session_id" ;;
    gemini) require_invocation "gemini|--resume $session_id" ;;
  esac
done

for label in codex-a codex-b claude-a claude-b gemini-a gemini-b; do
  tmux list-windows -t "$session" -F '#{window_name}' | grep -Fqx "$label"
done
echo "[OK] restore launched all six exact provider conversations"

echo "=== Exact Multi-Session Resume Integration Complete ==="
