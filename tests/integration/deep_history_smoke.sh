#!/usr/bin/env bash
set -euo pipefail

if ! command -v tmux >/dev/null 2>&1; then
    printf '%s\n' 'tmux is not installed; skipping deep-history integration test'
    exit 0
fi

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
DEEP_HISTORY_BIN="${CODEXFARM_DEEP_HISTORY_BIN:-${XDG_DATA_HOME:-$HOME/.local/share}/codexfarm/plugins/tmux-deep-history/bin/tmux-deep-history}"
if [[ ! -x "$DEEP_HISTORY_BIN" ]]; then
    printf 'tmux-deep-history executable not found: %s\n' "$DEEP_HISTORY_BIN" >&2
    exit 1
fi

TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/codexfarm-deep-history-test.XXXXXX")"
SESSION="codexfarm-deep-history-$$"

cleanup() {
    tmux kill-server >/dev/null 2>&1 || true
    rm -rf "$TEMP_DIR"
}
trap cleanup EXIT INT TERM HUP

export HOME="$TEMP_DIR/home"
export XDG_CONFIG_HOME="$TEMP_DIR/config"
export XDG_DATA_HOME="$TEMP_DIR/data"
export XDG_STATE_HOME="$TEMP_DIR/state"
export TMUX_TMPDIR="$TEMP_DIR/tmux"
export PATH="$ROOT_DIR/bin:$TEMP_DIR/bin:$PATH"
export CODEX_SESSION="$SESSION"
export CODEX_STATE_BASENAME="codexfarm-integration"
export CODEXFARM_HISTORY_BACKEND="deep-history"
export CODEXFARM_DEEP_HISTORY_BIN="$DEEP_HISTORY_BIN"
export CODEX_ANNOTATOR_AUTOSTART=0
export CODEX_AUTOSERVICE_CHOICE=no
export CODEX_TIPS_PROMPT=0
mkdir -p "$HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME" "$TMUX_TMPDIR" \
    "$TEMP_DIR/bin" "$TEMP_DIR/project"
unset TMUX

cat > "$TEMP_DIR/bin/fake-agent" <<'EOF_AGENT'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' 'fake-agent-ready'
while IFS= read -r line; do
    printf 'agent-output:%s\n' "$line"
done
EOF_AGENT
chmod +x "$TEMP_DIR/bin/fake-agent"
export CODEX_CMD="$TEMP_DIR/bin/fake-agent"

OUTPUT="$("$ROOT_DIR/bin/codex-add" -d "$TEMP_DIR/project")"
grep -q 'History backend: deep-history' <<< "$OUTPUT"

PANE_ID="$(tmux display-message -p -t "$SESSION:1" '#{pane_id}')"
[[ "$(tmux display-message -p -t "$PANE_ID" '#{pane_pipe}')" == "1" ]]
[[ "$(tmux show-options -pqv -t "$PANE_ID" '@deep-history-owned')" == "1" ]]
[[ "$(tmux show-options -gqv '@deep-history-auto-start')" == "off" ]]

tmux send-keys -t "$PANE_ID" 'integration-marker' Enter

FARM_LOG="$(find "$XDG_STATE_HOME/codexfarm-integration/logs" -type f -name '*.log' -print -quit)"
FOUND=0
for _ in $(seq 1 100); do
    if grep -a -q 'agent-output:integration-marker' "$FARM_LOG" 2>/dev/null \
        && grep -R -a -q 'agent-output:integration-marker' "$XDG_STATE_HOME/tmux/deep-history" 2>/dev/null; then
        FOUND=1
        break
    fi
    sleep 0.05
done
if [[ "$FOUND" != "1" ]]; then
    printf '%s\n' 'live output did not reach both deep history and the farm mirror' >&2
    exit 1
fi

RUN_DIR="$(tmux show-options -pqv -t "$PANE_ID" '@deep-history-run-dir')"
python3 - "$RUN_DIR/metadata.json" "$FARM_LOG" <<'PY'
import json
import pathlib
import sys

metadata = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert metadata["status"] == "active", metadata
assert metadata["mirror_output"] == str(pathlib.Path(sys.argv[2]).resolve()), metadata
assert metadata["mirror_bytes"] > 0, metadata
assert pathlib.Path(sys.argv[2]).stat().st_mode & 0o777 == 0o600
PY

# Exercise the teardown path Agent CLI Farm actually uses. The pinned plugin's
# standalone `stop` command and logger both update metadata, so a fast logger
# exit can be overwritten from `closed` back to `closing`. Farm reboot instead
# destroys managed windows/sessions, which closes tmux's pipe without that
# competing writer.
tmux kill-window -t "$SESSION:1"
CLOSED=0
for _ in $(seq 1 200); do
    if grep -q '"status": "closed"' "$RUN_DIR/metadata.json" 2>/dev/null; then
        CLOSED=1
        break
    fi
    sleep 0.05
done
if [[ "$CLOSED" != "1" ]]; then
    printf '%s\n' 'deep-history metadata did not close within 10 seconds' >&2
    cat "$RUN_DIR/metadata.json" >&2 || true
    exit 1
fi

printf '%s\n' 'agent-cli-farm deep-history integration test passed'
