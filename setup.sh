#!/usr/bin/env bash
set -euo pipefail

# Codex CLI Farm Setup Script
# Installs dependencies and installs helper scripts for managing Codex instances in tmux

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
fi

# Install helper scripts from this repo
echo "Installing helper scripts..."
mkdir -p "$HOME/bin" "${XDG_STATE_HOME:-$HOME/.local/state}/codexfarm/logs"

cp -f bin/codex-* "$HOME/bin/"
chmod +x "$HOME/bin"/codex-*

echo "Helper scripts installed in $HOME/bin:"
ls -1 "$HOME/bin"/codex-* | sed 's/^/  - /'

echo ""
echo "Make sure $HOME/bin is in your PATH:"
echo "  export PATH=\"$HOME/bin:$PATH\""
echo ""
echo "Usage examples:"
echo "  codex-add                    # Start Codex in current directory"
echo "  codex-add -d /path/project   # Start without attaching"
echo "  codex-adopt -d 12345         # Adopt process without attaching"
echo "  codex-save                   # Snapshot current windows to manifest"
echo "  codex-restore -a             # Restore windows and attach"
echo "  codex-watch                  # Watch all Codex logs"
