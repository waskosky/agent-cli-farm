#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v tmux >/dev/null 2>&1; then
    echo "tmux not found; install tmux to run this demo." >&2
    exit 127
fi

tmp_root="$(mktemp -d)"
session="codexfarm-demo-$$"
board_session="${session}-board"

cleanup() {
    set +e
    tmux kill-session -t "$session" >/dev/null 2>&1 || true
    tmux kill-session -t "$board_session" >/dev/null 2>&1 || true
    if [ -n "${TMUX_TMPDIR:-}" ] && [ -d "$TMUX_TMPDIR" ]; then
        tmux kill-server >/dev/null 2>&1 || true
    fi
    rm -rf "$tmp_root"
}
trap cleanup EXIT HUP INT TERM

export HOME="$tmp_root/home"
export XDG_CONFIG_HOME="$tmp_root/config"
export XDG_STATE_HOME="$tmp_root/state"
export TMUX_TMPDIR="$tmp_root/tmux"
export PATH="$repo_root/bin:$tmp_root/bin:$PATH"
export CODEX_SESSION="$session"
export CODEX_STATE_BASENAME="codexfarm-demo-$$"
export CODEX_TIPS_PROMPT=0
export CODEX_AUTOSERVICE_CHOICE=no
export CODEX_ANNOTATOR_AUTOSTART=0
unset TMUX

mkdir -p "$HOME" "$XDG_CONFIG_HOME" "$XDG_STATE_HOME" "$TMUX_TMPDIR" "$tmp_root/bin"

mock_agent="$tmp_root/bin/mock-codex"
cat > "$mock_agent" <<'EOF'
#!/usr/bin/env bash
trap 'exit 0' TERM INT
printf 'mock agent running in %s\n' "$PWD"
while :; do sleep 1; done
EOF
chmod +x "$mock_agent"
export CODEX_CMD="$mock_agent"
export CODEX_ARGS=""

project1="$tmp_root/projects/project1"
project2="$tmp_root/projects/project2"
manifest="$tmp_root/manifest.tsv"
mkdir -p "$project1" "$project2"
printf '# Demo Project 1\n' > "$project1/README.md"
printf '# Demo Project 2\n' > "$project2/README.md"

echo "=== Agent CLI Farm Demo ==="
echo "session: $session"
echo ""

echo "1. Adding first mock instance..."
CODEX_NAME="project1" "$repo_root/bin/codex-add" -d "$project1"

echo ""
echo "2. Adding second mock instance..."
CODEX_NAME="project2" "$repo_root/bin/codex-add" -d "$project2"

echo ""
echo "3. Checking status..."
"$repo_root/bin/codex-status" sessions
echo ""
"$repo_root/bin/codex-status" windows

echo ""
echo "4. Creating and linking board..."
"$repo_root/bin/codex-board" create
"$repo_root/bin/codex-board" link

echo ""
echo "5. Saving manifest..."
"$repo_root/bin/codex-save" "$manifest"

echo ""
echo "6. Restoring from manifest..."
tmux kill-session -t "$session"
"$repo_root/bin/codex-restore" "$manifest"

echo ""
echo "7. Checking restored status..."
"$repo_root/bin/codex-status" windows

echo ""
echo "Demo complete. Temporary data was isolated under:"
echo "  $tmp_root"
echo "It will be removed when this script exits."
