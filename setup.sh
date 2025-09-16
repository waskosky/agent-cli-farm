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

# Ensure $HOME/bin is on PATH now and for future shells
echo ""
echo "Configuring PATH to include $HOME/bin..."

# Immediate for this shell
case ":$PATH:" in
  *:"$HOME/bin":*) ;;
  *) export PATH="$HOME/bin:$PATH" ;;
esac

# Prepare rc targets based on user's shell
SHELL_NAME="$(basename "${SHELL:-}")"
RC_FILES=()
case "$SHELL_NAME" in
  bash) RC_FILES+=("$HOME/.bashrc" "$HOME/.profile") ;;
  zsh)  RC_FILES+=("$HOME/.zshrc") ;;
  fish) RC_FILES+=("$HOME/.config/fish/config.fish") ;;
  *)    RC_FILES+=("$HOME/.profile") ;;
esac

updated_any=0
for rc in "${RC_FILES[@]}"; do
  dir_rc="$(dirname "$rc")"
  [ -d "$dir_rc" ] || mkdir -p "$dir_rc"
  if [[ "$rc" == *.fish ]]; then
    # Fish shell syntax
    if ! grep -qsE '(^|\s)\$HOME/bin(\s|:|$)' "$rc" && ! grep -qsE 'set -gx PATH .*\$HOME/bin' "$rc"; then
      {
        echo ""
        echo "# Added by Codex CLI Farm setup"
        echo "set -gx PATH \$HOME/bin \$PATH"
      } >> "$rc"
      updated_any=1
      echo "  Updated: $rc (fish PATH)"
    fi
  else
    # POSIX-ish shells (bash, zsh, sh, etc.)
    if [ -f "$rc" ]; then
      if ! grep -qsE '(^|:)\$HOME/bin(:|$)' "$rc" && ! grep -qs 'export PATH="\$HOME/bin:\$PATH"' "$rc"; then
        {
          echo ""
          echo "# Added by Codex CLI Farm setup"
          echo "export PATH=\"\$HOME/bin:\$PATH\""
        } >> "$rc"
        updated_any=1
        echo "  Updated: $rc"
      fi
    else
      {
        echo "# Created by Codex CLI Farm setup"
        if [ "$SHELL_NAME" = "zsh" ]; then
          echo "export PATH=\"\$HOME/bin:\$PATH\""
        else
          echo "# This file is sourced by your shell on startup"
          echo "export PATH=\"\$HOME/bin:\$PATH\""
        fi
      } > "$rc"
      updated_any=1
      echo "  Created: $rc"
    fi
  fi
done

if [ "$updated_any" -eq 1 ]; then
  echo "PATH configuration updated. Open a new shell or 'source' your rc file."
else
  echo "PATH already configured for your shell(s)."
fi

echo ""
echo "Usage examples (re-run ./setup.sh anytime to update scripts):"
echo "  codex-add                    # Start Codex in current directory"
echo "  codex-add -d /path/project   # Start without attaching"
echo "  codex-adopt -d 12345         # Adopt process without attaching"
echo "  codex-auto-adopt             # Auto-adopt all running codex/cursor"
echo "  codex-remove name|index      # Remove a window (add -p to purge logs)"
echo "  codex-save                   # Snapshot current windows to manifest"
echo "  codex-restore -a             # Restore windows and attach"
echo "  codex-watch                  # Watch all Codex logs"
