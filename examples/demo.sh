#!/usr/bin/env bash
set -euo pipefail

# Demo script showing how to use Codex CLI Farm
# This creates a demo session with mock Codex instances

cd "$(dirname "$0")/.."

echo "=== Codex CLI Farm Demo ==="
echo ""

# Check if tmux is available
if ! command -v tmux >/dev/null; then
    echo "❌ tmux not found. Please install it first."
    exit 1
fi

echo "Starting demo with mock Codex instances..."
echo ""

# Add to PATH for this demo
demo_root="$(pwd)"
export PATH="$demo_root/bin:$demo_root/examples:$PATH"

# Use mock-codex instead of real codex
export CODEX_CMD="mock-codex"

echo "1. Adding first Codex instance..."
mkdir -p /tmp/demo-project1
echo "# Demo Project 1" > /tmp/demo-project1/README.md
CODEX_NAME="project1" bin/codex-add -d /tmp/demo-project1 || true

echo ""
echo "2. Adding second Codex instance..."
mkdir -p /tmp/demo-project2  
echo "# Demo Project 2" > /tmp/demo-project2/README.md
CODEX_NAME="project2" bin/codex-add -d /tmp/demo-project2 || true

echo ""
echo "3. Checking status..."
bin/codex-status sessions
echo ""
bin/codex-status windows
echo ""

echo "4. Creating board session..."
bin/codex-board create
bin/codex-board link

echo ""
echo "5. Checking logs..."
sleep 2
bin/codex-status logs

echo ""
echo "6. Saving and restoring manifest..."
bin/codex-save
bin/codex-restore

echo ""
echo "Demo complete! The following sessions are now running:"
echo "- codexfarm: Main session with Codex instances"
echo "- board: Navigation session"
echo ""
echo "To interact with them:"
echo "  tmux attach -t codexfarm   # Main session"
echo "  tmux attach -t board       # Board session"
echo "  bin/codex-watch           # Watch all logs"
echo ""
echo "To clean up:"
echo "  tmux kill-session -t codexfarm"
echo "  tmux kill-session -t board"
