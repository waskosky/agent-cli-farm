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
mkdir -p "$home_dir" "$config_dir" "$state_dir" "$private_bin" "$project_dir" "$tmux_tmp"
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
session_id="019e1659-3a2f-7a40-95cf-5ac9dd7fe5d4"
session_file="$HOME/.codex/sessions/2026/07/15/rollout-$session_id.jsonl"
manifest="$config_dir/codexfarm/session-resume.tsv"
invocation_log="$tmp_root/codex-invocations.log"
ready_file="$tmp_root/provider-ready"
mock_codex="$private_bin/codex"
launcher="$private_bin/provider-launcher"

mkdir -p "$(dirname "$session_file")"
printf '{"type":"session_meta","payload":{"id":"%s"}}\n' "$session_id" > "$session_file"

cat > "$mock_codex" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$MOCK_CODEX_INVOCATION_LOG"
exec 9< "$MOCK_CODEX_SESSION_FILE"
: > "$MOCK_CODEX_READY_FILE"
trap 'exit 0' TERM INT
while :; do
  sleep 1
done
EOF
chmod +x "$mock_codex"

cat > "$launcher" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
"$MOCK_CODEX_BIN" "$@" &
provider_pid=$!
cleanup_provider() {
  kill "$provider_pid" >/dev/null 2>&1 || true
  wait "$provider_pid" >/dev/null 2>&1 || true
}
trap cleanup_provider EXIT HUP INT TERM
wait "$provider_pid"
EOF
chmod +x "$launcher"

export MOCK_CODEX_BIN="$mock_codex"
export MOCK_CODEX_INVOCATION_LOG="$invocation_log"
export MOCK_CODEX_READY_FILE="$ready_file"
export MOCK_CODEX_SESSION_FILE="$session_file"

wait_for_file() {
  local path="$1"
  local attempt=0
  while [ "$attempt" -lt 100 ]; do
    [ -e "$path" ] && return 0
    sleep 0.05
    attempt=$((attempt + 1))
  done
  echo "Timed out waiting for $path" >&2
  return 1
}

wait_for_invocation() {
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

echo "=== Exact Session Resume Integration ==="

CODEX_SESSION="$session" \
  CODEX_NAME="resume-smoke" \
  CODEX_CMD="$launcher" \
  "$repo_root/bin/codex-add" -d "$project_dir" >/dev/null
wait_for_file "$ready_file"

CODEX_SESSION="$session" "$repo_root/bin/codex-save" "$manifest" >/dev/null
if ! awk -F '\t' -v expected="resume $session_id" '
  $3 == "codex" && $4 == expected { found = 1 }
  END { exit(found ? 0 : 1) }
' "$manifest"; then
  echo "Save did not discover the exact session through the real pane process tree" >&2
  exit 1
fi
if grep -Fq $'\tcodex\tresume --last' "$manifest"; then
  echo "Save unexpectedly used the Codex fallback" >&2
  exit 1
fi
echo "[OK] save discovered the exact session from a live provider file descriptor"

tmux kill-session -t "$session"
: > "$invocation_log"
rm -f "$ready_file"

CODEX_SESSION="$session" "$repo_root/bin/codex-restore" "$manifest" >/dev/null
wait_for_file "$ready_file"
wait_for_invocation "resume $session_id"
tmux list-windows -t "$session" -F '#{window_name}' | grep -Fqx "resume-smoke"
echo "[OK] restore launched the provider with the same exact session ID"

echo "=== Exact Session Resume Integration Complete ==="
