#!/usr/bin/env bash
set -euo pipefail

# Codex CLI Farm Setup Script
# Installs dependencies and installs helper scripts for managing Codex instances in tmux

echo "Setting up Codex CLI Farm..."

# Install tmux + multitail (runs whichever package manager exists)
echo "Installing dependencies..."
if command -v apt >/dev/null; then
  sudo apt update && sudo apt install -y tmux multitail
elif command -v dnf >/dev/null; then
  sudo dnf install -y tmux multitail
elif command -v yum >/dev/null; then
  sudo yum install -y tmux multitail
elif command -v pacman >/dev/null; then
  sudo pacman -Sy --noconfirm tmux multitail
elif command -v zypper >/dev/null; then
  sudo zypper install -y tmux multitail
else
  echo "No supported package manager found. Please install tmux and multitail manually."
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
shopt -s nullglob

create_claude_wrapper() {
  local wrapper="$1"
  local target="${wrapper#claude-}"
  target="codex-$target"
  local dest="$HOME/bin/$wrapper"
  cat > "$dest" <<EOF
#!/usr/bin/env bash
CODEX_TOOL_NAME=claude exec "\$(cd "\$(dirname "\$0")" && pwd)/$target" "\$@"
EOF
  chmod +x "$dest"
}

# Install helper scripts from this repo
echo "Installing helper scripts..."
mkdir -p "$HOME/bin" "${XDG_STATE_HOME:-$HOME/.local/state}/codexfarm/logs"

# Copy helper scripts (codex-* plus claude-* wrappers) except removed adopt helpers
skipped=()
copied=()
missing=()
scripts_to_copy=( "$SCRIPT_DIR"/bin/codex-* )
if [ -e "$SCRIPT_DIR/bin/codex-annotator.py" ]; then
  scripts_to_copy+=( "$SCRIPT_DIR/bin/codex-annotator.py" )
fi
for wrapper in claude-add claude-annotator claude-board claude-restore claude-resume claude-save claude-status claude-watch; do
  if [ -e "$SCRIPT_DIR/bin/$wrapper" ]; then
    scripts_to_copy+=( "$SCRIPT_DIR/bin/$wrapper" )
  else
    missing+=("$wrapper")
  fi
done

for f in "${scripts_to_copy[@]}"; do
  base=$(basename "$f")
  case "$base" in
    codex-adopt|codex-auto-adopt)
      skipped+=("$base")
      continue
      ;;
  esac
  cp -f "$f" "$HOME/bin/"
  chmod +x "$HOME/bin/$base"
  copied+=("$base")
done

# If any claude wrappers were missing from the repo (e.g., older checkout), generate them directly
if [ ${#missing[@]} -gt 0 ]; then
  for wrapper in "${missing[@]}"; do
    create_claude_wrapper "$wrapper"
    copied+=("$wrapper")
  done
  # Clear missing since we've generated them
  missing=()
fi

echo "Helper scripts installed in $HOME/bin:"
for s in "${copied[@]}"; do
  echo "  - $s"
done
if [ ${#missing[@]} -gt 0 ]; then
  echo "Missing helper scripts (not copied): ${missing[*]}" >&2
fi
if [ ${#skipped[@]} -gt 0 ]; then
  echo "(skipped removed commands: ${skipped[*]})"
fi

# Clean up legacy/removed commands from previous installs
for legacy in codex-adopt codex-auto-adopt; do
  if [ -e "$HOME/bin/$legacy" ]; then
    rm -f "$HOME/bin/$legacy" && echo "Removed legacy command from $HOME/bin: $legacy"
  fi
done

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
  echo "PATH configuration updated in your shell rc file(s)."
else
  echo "PATH already configured for your shell(s)."
fi

# Attempt to auto-reload the current shell session when sourced
# Detect if this script is being sourced (bash/zsh)
is_sourced=0
if [ -n "${BASH_SOURCE:-}" ] && [ "${BASH_SOURCE[0]:-}" != "$0" ]; then
  is_sourced=1
elif [ -n "${ZSH_EVAL_CONTEXT:-}" ] && [[ "$ZSH_EVAL_CONTEXT" == *:file ]]; then
  is_sourced=1
fi

if [ "$is_sourced" -eq 1 ]; then
  for rc in "${RC_FILES[@]}"; do
    [ -f "$rc" ] || continue
    # shellcheck disable=SC1090
    . "$rc" || true
  done
  hash -r 2>/dev/null || true
  echo "Reloaded current shell session (via source)."
else
  # Not sourced: we can't modify the parent shell's environment.
  # Provide a precise next step to the user.
  primary_rc="${RC_FILES[0]}"
  echo ""
  echo "To apply changes in this shell now, run:"
  if [ -n "$primary_rc" ] && [ -f "$primary_rc" ]; then
    echo "  source $primary_rc"
  else
    echo "  exec \"$SHELL\" -l"
  fi
fi

echo ""
echo "Usage examples (re-run ./setup.sh anytime to update scripts):"
echo "  codex-add                    # Start Codex in current directory"
echo "  codex-add -d /path/project   # Start without attaching"
echo "  codex-save                   # Snapshot current windows to manifest"
echo "  codex-restore -a             # Restore windows and attach"
echo "  codex-resume                 # Attach/switch to existing session"
echo "  codex-watch                  # Watch all Codex logs"
echo "  claude-add                   # Start Claude in the shared Codex tmux session"
