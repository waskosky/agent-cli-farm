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

# Create codex-status script
cat >"$HOME/bin/codex-status" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

show_usage() {
    echo "Usage: $(basename "$0") [sessions|windows|logs]"
    echo "Show status information for Codex CLI Farm"
    echo ""
    echo "Commands:"
    echo "  sessions  Show tmux sessions"
    echo "  windows   Show Codex windows in the main session"
    echo "  logs      Show log files"
    echo ""
    echo "If no command is provided, shows all information"
}

show_sessions() {
    echo "=== Tmux Sessions ==="
    if tmux list-sessions 2>/dev/null | grep -q .; then
        tmux list-sessions | while IFS=: read -r name rest; do
            windows=$(echo "$rest" | sed 's/.*(\([0-9]*\) windows).*/\1/')
            created=$(echo "$rest" | sed 's/.*(created \([^)]*\)).*/\1/')
            echo "$name: $windows windows (created $created)"
        done
        echo ""
        SESSION="${CODEX_SESSION:-codexfarm}"
        if tmux has-session -t "$SESSION" 2>/dev/null; then
            echo "Main Codex session '$SESSION' is running"
        else
            echo "Main Codex session '$SESSION' is not running"
        fi
        if tmux has-session -t board 2>/dev/null; then
            echo "Board session is running"
        else
            echo "Board session is not running"
        fi
    else
        echo "No tmux sessions running"
    fi
}

show_windows() {
    echo "=== Codex Windows ==="
    SESSION="${CODEX_SESSION:-codexfarm}"
    if tmux has-session -t "$SESSION" 2>/dev/null; then
        tmux list-windows -t "$SESSION" -F "#{window_index}: #{window_name} (#{pane_current_path})"
    else
        echo "Main session '$SESSION' is not running"
    fi
}

show_logs() {
    echo "=== Log Files ==="
    LOGDIR="${XDG_STATE_HOME:-$HOME/.local/state}/codexfarm/logs"
    if [ -d "$LOGDIR" ]; then
        shopt -s nullglob
        files=( "$LOGDIR"/*.log )
        if [ ${#files[@]} -gt 0 ]; then
            for file in "${files[@]}"; do
                mtime=$(stat -c %y "$file" 2>/dev/null || stat -f %Sm "$file" 2>/dev/null || echo "unknown time")
                echo "${mtime%.*} $file"
            done
            echo ""
            echo "Total logs: ${#files[@]}"
        else
            echo "No log files found in $LOGDIR"
        fi
    else
        echo "Log directory $LOGDIR does not exist"
    fi
}

case "${1:-all}" in
    sessions) show_sessions ;;
    windows) show_windows ;;
    logs) show_logs ;;
    all) show_sessions; echo ""; show_windows; echo ""; show_logs ;;
    *) show_usage; exit 1 ;;
esac
EOF
chmod +x "$HOME/bin/codex-status"

# Create codex-board script
cat >"$HOME/bin/codex-board" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

show_usage() {
    echo "Usage: $(basename "$0") [create|link|switch]"
    echo "Manage board session for fast navigation between Codex instances"
    echo ""
    echo "Commands:"
    echo "  create  Create a new board session"
    echo "  link    Link all Codex windows to the board session"
    echo "  switch  Switch to the board session"
    echo ""
    echo "The board session provides a separate tmux session for quick navigation"
    echo "while keeping the main Codex session running independently."
}

create_board() {
    if tmux has-session -t board 2>/dev/null; then
        echo "Board session already exists"
        return 0
    fi
    tmux new-session -d -s board -n navigation
    echo "Created board session"
}

link_windows() {
    SESSION="${CODEX_SESSION:-codexfarm}"
    if ! tmux has-session -t "$SESSION" 2>/dev/null; then
        echo "Main session '$SESSION' is not running"
        return 1
    fi
    
    if ! tmux has-session -t board 2>/dev/null; then
        echo "Board session does not exist. Creating it..."
        create_board
    fi
    
    # Link all windows from main session to board
    tmux list-windows -t "$SESSION" -F "#{window_index}" | while read -r win; do
        # Check if window is already linked
        if ! tmux list-windows -t board | grep -q ":$win\*"; then
            tmux link-window -s "$SESSION:$win" -t board: 2>/dev/null || true
        fi
    done
    echo "Linked all Codex windows to board session"
}

switch_to_board() {
    if ! tmux has-session -t board 2>/dev/null; then
        echo "Board session does not exist. Create it first with: $(basename "$0") create"
        return 1
    fi
    
    if [ -n "${TMUX:-}" ]; then
        tmux switch-client -t board
    else
        tmux attach -t board
    fi
}

case "${1:-}" in
    create) create_board ;;
    link) link_windows ;;
    switch) switch_to_board ;;
    "") show_usage; exit 1 ;;
    *) show_usage; exit 1 ;;
esac
EOF
chmod +x "$HOME/bin/codex-board"

echo "Helper scripts created in $HOME/bin:"
echo "  - codex-add: Add new Codex instances to tmux"
echo "  - codex-adopt: Adopt existing Codex processes with reptyr"
echo "  - codex-watch: Monitor all Codex logs"
echo "  - codex-status: Show status of sessions, windows, and logs"
echo "  - codex-board: Manage board session for navigation"
echo ""
echo "Make sure $HOME/bin is in your PATH:"
echo "  export PATH=\"\$HOME/bin:\$PATH\""
echo ""
echo "Usage examples:"
echo "  codex-add                    # Start Codex in current directory"
echo "  codex-add /path/to/project   # Start Codex in specific directory"
echo "  codex-adopt 12345            # Adopt existing Codex process with PID 12345"
echo "  codex-watch                  # Watch all Codex logs"
echo "  codex-status sessions        # Show tmux sessions"
echo "  codex-board create           # Create navigation board"