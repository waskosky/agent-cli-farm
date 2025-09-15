#!/usr/bin/env bash
set -euo pipefail

# Codex CLI Farm Setup Script
# Installs dependencies and creates helper scripts for managing Codex instances in tmux

echo "Setting up Codex CLI Farm..."

# Install tmux + reptyr + multitail (runs whichever package manager exists)
echo "Installing dependencies..."
if command -v apt >/dev/null; then
  sudo apt update && sudo apt install -y tmux reptyr multitail
elif command -v dnf >/dev/null; then
  sudo dnf install -y tmux reptyr multitail
elif command -v yum >/dev/null; then
  sudo yum install -y tmux reptyr multitail
elif command -v pacman >/dev/null; then
  sudo pacman -Sy --noconfirm tmux reptyr multitail
elif command -v zypper >/dev/null; then
  sudo zypper install -y tmux reptyr multitail
else
  echo "No supported package manager found. Please install tmux, reptyr, and multitail manually."
  exit 1
fi

# Create helper scripts (no edits required)
echo "Creating helper scripts..."
mkdir -p "$HOME/bin" "${XDG_STATE_HOME:-$HOME/.local/state}/codexfarm/logs"

# Create codex-add script
cat >"$HOME/bin/codex-add" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
SESSION="${CODEX_SESSION:-codexfarm}"
DIR="${1:-$PWD}"
NAME="${CODEX_NAME:-$(basename "$DIR")}"
CMD="${CODEX_CMD:-codex}"
ARGS="${CODEX_ARGS:-}"
for c in tmux "$CMD"; do command -v "$c" >/dev/null || { echo "$c not found" >&2; exit 127; }; done
LOGDIR="${XDG_STATE_HOME:-$HOME/.local/state}/codexfarm/logs"; mkdir -p "$LOGDIR"
tmux has-session -t "$SESSION" 2>/dev/null || tmux new-session -d -s "$SESSION" -n home
WIN=$(tmux new-window -t "$SESSION" -P -F "#{window_index}" -n "$NAME" -c "$DIR" "$CMD $ARGS")
STAMP=$(date +%Y%m%d-%H%M%S); LOGFILE="$LOGDIR/${NAME}_${STAMP}.log"
tmux pipe-pane -o -t "$SESSION:$WIN" "cat >> \"$LOGFILE\""
tmux has-session -t board 2>/dev/null && tmux link-window -s "$SESSION:$WIN" -t board
if [ -n "${TMUX:-}" ]; then tmux select-window -t "$SESSION:$WIN"; else tmux attach -t "$SESSION"; fi
printf 'Started %s in %s:%s\nLogging to %s\n' "$CMD" "$SESSION" "$WIN" "$LOGFILE"
EOF
chmod +x "$HOME/bin/codex-add"

# Create codex-adopt script
cat >"$HOME/bin/codex-adopt" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[ $# -eq 1 ] || { echo "Usage: $(basename "$0") PID" >&2; exit 1; }
PID="$1"; SESSION="${CODEX_SESSION:-codexfarm}"
tmux has-session -t "$SESSION" 2>/dev/null || tmux new-session -d -s "$SESSION" -n home
DIR="$(readlink -f "/proc/$PID/cwd" 2>/dev/null || echo "$HOME")"; NAME="adopt-$PID"
WIN=$(tmux new-window -t "$SESSION" -P -F "#{window_index}" -n "$NAME" -c "$DIR")
tmux send-keys -t "$SESSION:$WIN" "reptyr -T -s $PID" C-m   # see Notes if this errors
LOGDIR="${XDG_STATE_HOME:-$HOME/.local/state}/codexfarm/logs"; mkdir -p "$LOGDIR"
STAMP=$(date +%Y%m%d-%H%M%S); LOGFILE="$LOGDIR/${NAME}_${STAMP}.log"
tmux pipe-pane -o -t "$SESSION:$WIN" "cat >> \"$LOGFILE\""
tmux has-session -t board 2>/dev/null && tmux link-window -s "$SESSION:$WIN" -t board
if [ -n "${TMUX:-}" ]; then tmux select-window -t "$SESSION:$WIN"; else tmux attach -t "$SESSION"; fi
printf 'Adopted PID %s into %s:%s\nLogging to %s\n' "$PID" "$SESSION" "$WIN" "$LOGFILE"
EOF
chmod +x "$HOME/bin/codex-adopt"

# Create codex-watch script
cat >"$HOME/bin/codex-watch" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
LOGDIR="${XDG_STATE_HOME:-$HOME/.local/state}/codexfarm/logs"; mkdir -p "$LOGDIR"
shopt -s nullglob; files=( "$LOGDIR"/*.log )
[ ${#files[@]} -gt 0 ] || { echo "No logs yet in $LOGDIR"; exit 0; }
if command -v multitail >/dev/null; then exec multitail -n 200 "${files[@]}"; fi
echo "multitail not found; falling back to tail"; exec tail -n 200 -F -q "${files[@]}"
EOF
chmod +x "$HOME/bin/codex-watch"

echo "Setup complete!"
echo "Helper scripts created in $HOME/bin:"
echo "  - codex-add: Add new Codex instances to tmux"
echo "  - codex-adopt: Adopt existing Codex processes with reptyr"
echo "  - codex-watch: Monitor all Codex logs"
echo ""
echo "Make sure $HOME/bin is in your PATH:"
echo "  export PATH=\"\$HOME/bin:\$PATH\""
echo ""
echo "Usage examples:"
echo "  codex-add                    # Start Codex in current directory"
echo "  codex-add /path/to/project   # Start Codex in specific directory"
echo "  codex-adopt 12345            # Adopt existing Codex process with PID 12345"
echo "  codex-watch                  # Watch all Codex logs"