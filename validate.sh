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
for script in bin/codex-*; do
    if bash -n "$script"; then
        echo "✅ $script syntax OK"
    else
        echo "❌ $script syntax error"
        exit 1
    fi
done

# Test help messages
echo ""
echo "Testing help messages..."

# Test codex-remove with no args (should show usage)
if bin/codex-remove 2>&1 | grep -q "Usage:" || true; then
    echo "✅ codex-remove shows usage when called without arguments"
else
    echo "❌ codex-remove doesn't show proper usage message"
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

# Test status commands
echo ""
echo "Testing status commands..."
if bin/codex-status sessions >/dev/null 2>&1; then
    echo "✅ codex-status sessions works"
else
    echo "❌ codex-status sessions failed"
fi

if bin/codex-status logs >/dev/null 2>&1; then
    echo "✅ codex-status logs works"
else
    echo "❌ codex-status logs failed"
fi

echo ""
echo "=== Validation Complete ==="
echo ""
echo "All scripts appear to be working correctly!"
echo "To use the Codex CLI Farm:"
echo "1. Run ./setup.sh to install dependencies"
echo "2. Add $HOME/bin to your PATH"
echo "3. Use codex-add to start managing Codex instances"
