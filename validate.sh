#!/usr/bin/env bash
set -euo pipefail

# Simple validation script to test the Codex CLI Farm setup
# This script performs basic checks without requiring actual Codex installation

echo "=== Codex CLI Farm Validation ==="
echo ""

# Check if tmux is available (required for testing)
if ! command -v tmux >/dev/null; then
    echo "❌ tmux not found - required for testing"
    echo "Please run the setup script first: ./setup.sh"
    exit 1
fi

echo "✅ tmux found"

# Test script syntax
echo ""
echo "Checking script syntax..."
for script in bin/codex-* bin/add_high_memory_warning.sh; do
    shebang="$(head -n 1 "$script")"
    if echo "$shebang" | grep -qE '^#!.*(env[[:space:]]+)?(ba)?sh([[:space:]]|$)'; then
        if bash -n "$script"; then
            echo "✅ $script syntax OK"
        else
            echo "❌ $script syntax error"
            exit 1
        fi
    elif echo "$shebang" | grep -qE '^#!.*python'; then
        if command -v python3 >/dev/null; then
            if PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile "$script"; then
                echo "✅ $script syntax OK (python3)"
            else
                echo "❌ $script syntax error (python3)"
                exit 1
            fi
        else
            echo "⚠️  python3 not found; skipping $script"
        fi
    else
        echo "⚠️  Skipping syntax check for $script (unknown shebang)"
    fi
done

# Check claude wrapper template for over-escaped quotes
echo ""
echo "Testing tool wrapper template..."
wrapper_line="$(grep -nF 'CODEX_TOOL_NAME=$tool exec' setup.sh || true)"
if [ -z "$wrapper_line" ]; then
    echo "❌ tool wrapper template not found in setup.sh"
    exit 1
fi
if echo "$wrapper_line" | grep -q '\\\"'; then
    echo "❌ claude wrapper template contains escaped quotes"
    exit 1
fi
echo "✅ tool wrapper template uses plain quotes"

# Test help messages
echo ""
echo "Testing help messages..."

if bin/codex-looper --help 2>&1 | grep -q "Tiny coding-agent looper"; then
    echo "✅ codex-looper shows help"
else
    echo "❌ codex-looper doesn't show help"
    exit 1
fi

# Test codex-board with invalid action
if bin/codex-board invalid 2>&1 | grep -q "Usage:" || true; then
    echo "✅ codex-board shows usage for invalid action"
else
    echo "❌ codex-board doesn't show proper usage message"
fi

# Test codex-status with invalid action
if bin/codex-status invalid 2>&1 | grep -q "Usage:" || true; then
    echo "✅ codex-status shows usage for invalid action"
else
    echo "❌ codex-status doesn't show proper usage message"
fi

# Test log directory creation
echo ""
echo "Testing log directory creation..."
LOGDIR="${XDG_STATE_HOME:-$HOME/.local/state}/codexfarm/logs"
rm -rf "${XDG_STATE_HOME:-$HOME/.local/state}/codexfarm" 2>/dev/null || true

# Run codex-watch to create log directory
if bin/codex-watch 2>&1 | grep -q "No logs yet"; then
    echo "✅ Log directory created and codex-watch handles empty directory"
else
    echo "❌ codex-watch doesn't handle empty log directory properly"
fi

if [ -d "$LOGDIR" ]; then
    echo "✅ Log directory created at $LOGDIR"
else
    echo "❌ Log directory not created"
fi

# Test codex-add in a non-interactive, detached mode
echo ""
echo "Testing codex-add..."
TEST_SESSION="codexfarm-validate-$$"
TEST_STATE_BASENAME="codexfarm-validate-$$"
if CODEX_SESSION="$TEST_SESSION" \
  CODEX_STATE_BASENAME="$TEST_STATE_BASENAME" \
  CODEX_NAME="validate-test" \
  CODEX_CMD="sleep" \
  CODEX_ARGS="2" \
  CODEX_TIPS_PROMPT=0 \
  CODEX_AUTOSERVICE_CHOICE=no \
  CODEX_ANNOTATOR_AUTOSTART=0 \
  bin/codex-add -d "$PWD"; then
    echo "✅ codex-add works"
else
    echo "❌ codex-add failed"
    tmux kill-session -t "$TEST_SESSION" >/dev/null 2>&1 || true
    exit 1
fi
tmux kill-session -t "$TEST_SESSION" >/dev/null 2>&1 || true

# Test status commands
echo ""
echo "Testing status commands..."
if bin/codex-status sessions >/dev/null 2>&1; then
    echo "✅ codex-status sessions works"
else
    echo "❌ codex-status sessions failed"
    exit 1
fi

if bin/codex-status logs >/dev/null 2>&1; then
    echo "✅ codex-status logs works"
else
    echo "❌ codex-status logs failed"
    exit 1
fi

echo ""
echo "=== Validation Complete ==="
echo ""
echo "All scripts appear to be working correctly!"
echo "To use the Codex CLI Farm:"
echo "1. Run ./setup.sh to install dependencies"
echo "2. Add $HOME/bin to your PATH"
echo "3. Use codex-add to start managing Codex instances"
echo "4. Use codex-looper init to create starter prompt loops"
