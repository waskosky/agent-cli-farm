#!/usr/bin/env bash

# Agent CLI Farm Setup Script
# Installs helper scripts for managing coding-agent instances in tmux.

codexfarm_setup_main() (
  set -euo pipefail

  local install_deep_history=0
  local setup_python=""
  while (( "$#" )); do
    case "${1:-}" in
      --with-deep-history)
        install_deep_history=1
        ;;
      -h|--help)
        cat <<'EOF'
Usage: setup.sh [--with-deep-history]

Install Agent CLI Farm helpers. The optional flag also installs the
checksum-pinned tmux-deep-history release used by the automatic history backend.
EOF
        exit 0
        ;;
      *)
        echo "Unknown setup option: $1" >&2
        exit 2
        ;;
    esac
    shift
  done
  case "${CODEXFARM_WITH_DEEP_HISTORY:-}" in
    1|true|TRUE|True|yes|YES|Yes|on|ON|On) install_deep_history=1 ;;
  esac

  echo "Setting up Agent CLI Farm..."

  have_command() {
    command -v "$1" >/dev/null 2>&1
  }

  have_executable() {
    case "$1" in
      */*) [ -x "$1" ] ;;
      *) have_command "$1" ;;
    esac
  }

  python_is_supported() {
    "$1" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
  }

  find_python() {
    local candidate
    for candidate in \
      "${CODEXFARM_PYTHON_BIN:-}" \
      python3 \
      python3.14 \
      python3.13 \
      python3.12 \
      python3.11 \
      python3.10
    do
      [ -n "$candidate" ] || continue
      have_executable "$candidate" || continue
      if python_is_supported "$candidate"; then
        printf '%s\n' "$candidate"
        return 0
      fi
    done
    return 1
  }

  require_python() {
    if ! setup_python="$(find_python)"; then
      echo "Python 3.10 or newer is required; install python3.10+ or set CODEXFARM_PYTHON_BIN." >&2
      exit 1
    fi
    echo "Using Python interpreter: $setup_python"
  }

  install_dependencies() {
    local missing=()

    have_command tmux || missing+=("tmux")
    have_command multitail || missing+=("multitail")

    if [ "${#missing[@]}" -eq 0 ]; then
      echo "Dependencies already available; skipping package installation."
      return 0
    fi

    echo "Missing commands: ${missing[*]}"
    echo "Dependency installation skipped; install missing commands separately for full functionality."

    if have_command tmux; then
      echo "tmux is available."
    else
      echo "tmux is still missing; core tmux commands will not work until you install it."
    fi

    if have_command multitail; then
      echo "multitail is available."
    else
      echo "multitail is still missing; codex-watch will fall back to simple tail mode."
    fi
  }

  add_unique_script() {
    local candidate="$1"
    local existing
    [ -e "$candidate" ] || return 0
    if [ "${#scripts_to_copy[@]}" -gt 0 ]; then
      for existing in "${scripts_to_copy[@]}"; do
        [ "$existing" = "$candidate" ] && return 0
      done
    fi
    scripts_to_copy+=("$candidate")
  }

  create_tool_wrapper() {
    local tool="$1"
    local wrapper="$2"
    local target="${wrapper#"${tool}"-}"
    local dest="$HOME/bin/$wrapper"
    target="codex-$target"
    cat > "$dest" <<EOF
#!/usr/bin/env bash
CODEX_TOOL_NAME=$tool exec "\$(cd "\$(dirname "\$0")" && pwd)/$target" "\$@"
EOF
    chmod +x "$dest"
  }

  append_path_config() {
    local rc="$1"
    local shell_name="$2"
    local dir_rc
    dir_rc="$(dirname "$rc")"
    [ -d "$dir_rc" ] || mkdir -p "$dir_rc"

    if [ "${rc##*.}" = "fish" ]; then
      if ! grep -qsE '(^|\s)\$HOME/bin(\s|:|$)' "$rc" \
        && ! grep -qsE 'set -gx PATH .*\$HOME/bin' "$rc"; then
        {
          echo ""
          echo "# Added by Agent CLI Farm setup"
          echo "set -gx PATH \$HOME/bin \$PATH"
        } >> "$rc"
        echo "  Updated: $rc (fish PATH)"
        return 1
      fi
      return 0
    fi

    if [ -f "$rc" ]; then
      if ! grep -qsE '(^|:)\$HOME/bin(:|$)' "$rc" \
        && ! grep -qs 'export PATH="\$HOME/bin:\$PATH"' "$rc"; then
        {
          echo ""
          echo "# Added by Agent CLI Farm setup"
          echo "export PATH=\"\$HOME/bin:\$PATH\""
        } >> "$rc"
        echo "  Updated: $rc"
        return 1
      fi
      return 0
    fi

    {
      echo "# Created by Agent CLI Farm setup"
      if [ "$shell_name" != "zsh" ]; then
        echo "# This file is sourced by your shell on startup"
      fi
      echo "export PATH=\"\$HOME/bin:\$PATH\""
    } > "$rc"
    echo "  Created: $rc"
    return 1
  }

  require_python
  install_dependencies

  local script_source script_dir shell_name updated_any primary_rc
  script_source="${BASH_SOURCE[0]}"
  script_dir="$(cd "$(dirname "$script_source")" && pwd)"

  echo "Installing helper scripts..."
  mkdir -p "$HOME/bin" "${XDG_STATE_HOME:-$HOME/.local/state}/codexfarm/logs"

  local scripts_to_copy=()
  local wrapper_tools=(claude gemini)
  local wrapper_suffixes=(add annotator board farm-reboot restore resume save status watch looper)
  local skipped=()
  local copied=()
  local missing=()
  local f base tool suffix wrapper legacy rc dir_rc

  for f in "$script_dir"/bin/codex-*; do
    add_unique_script "$f"
  done
  add_unique_script "$script_dir/bin/add_high_memory_warning.sh"
  add_unique_script "$script_dir/bin/codex-annotator.py"

  for tool in "${wrapper_tools[@]}"; do
    for suffix in "${wrapper_suffixes[@]}"; do
      wrapper="$tool-$suffix"
      if [ -e "$script_dir/bin/$wrapper" ]; then
        add_unique_script "$script_dir/bin/$wrapper"
      else
        missing+=("$wrapper")
      fi
    done
  done

  for f in "${scripts_to_copy[@]}"; do
    base="$(basename "$f")"
    case "$base" in
      codex-adopt|codex-auto-adopt)
        skipped+=("$base")
        continue
        ;;
    esac
    cp -f "$f" "$HOME/bin/$base"
    chmod +x "$HOME/bin/$base"
    copied+=("$base")
  done

  if [ -d "$script_dir/codex_looper" ]; then
    mkdir -p "$HOME/bin/codex_looper"
    cp -f "$script_dir"/codex_looper/*.py "$HOME/bin/codex_looper/"
    copied+=("codex_looper/")
  fi

  if [ "${#missing[@]}" -gt 0 ]; then
    for wrapper in "${missing[@]}"; do
      create_tool_wrapper "${wrapper%%-*}" "$wrapper"
      copied+=("$wrapper")
    done
    missing=()
  fi

  echo "Helper scripts installed in $HOME/bin:"
  for base in "${copied[@]}"; do
    echo "  - $base"
  done
  if [ "${#skipped[@]}" -gt 0 ]; then
    echo "(skipped removed commands: ${skipped[*]})"
  fi

  for legacy in codex-adopt codex-auto-adopt; do
    if [ -e "$HOME/bin/$legacy" ]; then
      rm -f "$HOME/bin/$legacy" && echo "Removed legacy command from $HOME/bin: $legacy"
    fi
  done

  if [ "$install_deep_history" -eq 1 ]; then
    local deep_history_installer_args=()
    if [ -n "${CODEXFARM_DEEP_HISTORY_LOCK_FILE:-}" ]; then
      deep_history_installer_args+=(--lock-file "$CODEXFARM_DEEP_HISTORY_LOCK_FILE")
    fi
    if [ -n "${CODEXFARM_DEEP_HISTORY_ARCHIVE:-}" ]; then
      deep_history_installer_args+=(--archive "$CODEXFARM_DEEP_HISTORY_ARCHIVE")
    fi
    if [ -n "${CODEXFARM_DEEP_HISTORY_DESTINATION:-}" ]; then
      deep_history_installer_args+=(--destination "$CODEXFARM_DEEP_HISTORY_DESTINATION")
    fi
    echo ""
    echo "Installing pinned tmux-deep-history integration..."
    "$setup_python" "$script_dir/integrations/install_tmux_deep_history.py" \
      "${deep_history_installer_args[@]}"
  fi

  echo ""
  echo "Configuring PATH to include $HOME/bin..."

  shell_name="$(basename "${SHELL:-}")"
  local rc_files=()
  case "$shell_name" in
    bash) rc_files+=("$HOME/.bashrc" "$HOME/.profile") ;;
    zsh) rc_files+=("$HOME/.zshrc") ;;
    fish) rc_files+=("$HOME/.config/fish/config.fish") ;;
    *) rc_files+=("$HOME/.profile") ;;
  esac

  updated_any=0
  for rc in "${rc_files[@]}"; do
    if ! append_path_config "$rc" "$shell_name"; then
      updated_any=1
    fi
  done

  if [ "$updated_any" -eq 1 ]; then
    echo "PATH configuration updated in your shell rc file(s)."
  else
    echo "PATH already configured for your shell(s)."
  fi

  primary_rc="${rc_files[0]:-}"
  echo ""
  echo "Usage examples (re-run ./setup.sh anytime to update scripts):"
  echo "  codex-add                    # Start Codex in current directory"
  echo "  codex-add work               # Start current directory in the 'work' farm"
  echo "  codex-add work /path/project # Start a project in the 'work' farm"
  echo "  codex-add -d /path/project   # Start without attaching"
  echo "  codex-save                   # Snapshot current windows to manifest"
  echo "  codex-farm-reboot            # Save, restart, and restore the default farm"
  echo "  codex-restore -a             # Restore windows and attach"
  echo "  codex-resume                 # Attach/switch to existing session"
  echo "  codex-resume work --board    # Jump to the board for the 'work' farm"
  echo "  codex-memoryflag             # Flag tmux windows using 200 MiB RSS or more"
  echo "  codex-memoryflag 500         # Flag tmux windows using 500 MiB RSS or more"
  echo "  codex-watch                  # Watch all Codex logs"
  echo "  codex-looper                 # Run initialized prompt loops in the default farm"
  echo "  codex-looper init --interactive --force"
  echo "  codex-looper --local --once --label local-smoke"
  echo "  claude-add                   # Start Claude in the shared Codex tmux session"
  echo "  gemini-add                   # Start Gemini in the shared Codex tmux session"

  if [ "${CODEXFARM_SETUP_SOURCED:-0}" -eq 1 ]; then
    echo ""
    echo "Current shell PATH updated."
  else
    echo ""
    echo "To apply changes in this shell now, run:"
    if [ -n "$primary_rc" ] && [ -f "$primary_rc" ]; then
      echo "  source $primary_rc"
    else
      echo "  export PATH=\"\$HOME/bin:\$PATH\""
    fi
  fi
)

codexfarm_setup_is_sourced=0
if [ -n "${BASH_SOURCE:-}" ] && [ "${BASH_SOURCE[0]:-}" != "$0" ]; then
  codexfarm_setup_is_sourced=1
fi

if [ "$codexfarm_setup_is_sourced" -eq 1 ]; then
  CODEXFARM_SETUP_SOURCED=1 codexfarm_setup_main
  case ":$PATH:" in
    *:"$HOME/bin":*) ;;
    *) export PATH="$HOME/bin:$PATH" ;;
  esac
  hash -r 2>/dev/null || true
  unset -f codexfarm_setup_main
  unset codexfarm_setup_is_sourced
else
  codexfarm_setup_main "$@"
fi
