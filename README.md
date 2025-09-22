# Codex CLI Farm

A comprehensive tmux session management system for easily managing and restoring tmux sessions that auto-consolidate and run all your Codex CLI instances.

## Features

- **Automated session management**: Long-lived tmux session that persists across reboots
- **Centralized logging**: Each Codex pane logs to individual files with timestamps
- **Unified monitoring**: Watch all Codex instances from a single consolidated view
- **Fast navigation**: Optional "board" session for quick switching between instances
- **Cross-platform**: Works with all major Linux package managers
- **Snapshot/restore**: Save a manifest of windows and restore them later

## Quick Start

### 1. One-time Setup

Run the setup script to install dependencies and create helper scripts (source it to auto-reload your shell):

```bash
source ./setup.sh
```

This will:
- Install `tmux` and `multitail` using your system's package manager
- Create helper scripts in `$HOME/bin/`
- Set up logging directories
- Add `$HOME/bin` to your PATH automatically (bash/zsh/fish) and the current session

### 2. Add Codex Instances

From any project directory:
```bash
codex-add
```

Or specify a path:
```bash
codex-add /path/to/project
```

### 3. Save/Restore Your Farm

Snapshot your current Codex windows:
```bash
codex-save              # writes to ~/.config/codexfarm/manifest.tsv
```

Restore them later (e.g., after reboot or on SSH login):
```bash
codex-restore -a        # recreates and attaches to the session
```

Use `-f` to force re-creation of existing-named windows.

### 4. Watch All Instances

Monitor all Codex logs in real-time:
```bash
codex-watch
```
Notes:
- On small terminals (phones), `codex-watch` auto-switches to a simpler mode.
- Force simple mode: `codex-watch --simple` or `CODEX_WATCH_MODE=tail codex-watch`.
- Force full mode: `codex-watch --mode multitail`.
- First run shows an optional tmux tips prompt; choose Yes to see basics. Answer "Don't show again" to persist your preference. Re-enable temporarily with `CODEX_TIPS_PROMPT=1` or permanently by removing `~/.local/state/codexfarm/no_tips`.

## Available Commands

### Core Commands

- **`codex-add [directory]`** - Add new Codex instance to tmux session
- **`codex-watch`** - Monitor all Codex logs in consolidated view
- **`codex-status [sessions|windows|logs]`** - Show status information
- **`codex-board [create|link|switch]`** - Manage board session for navigation
- **`codex-save [manifest]`** - Snapshot current windows to a manifest (TSV)
- **`codex-restore [-a] [-f] [manifest]`** - Restore windows from a manifest

### Environment Variables

You can customize behavior with these environment variables:

- **`CODEX_SESSION`** - tmux session name (default: `codexfarm`)
- **`CODEX_NAME`** - window name (default: directory basename)
- **`CODEX_CMD`** - command to run (default: `codex`)
- **`CODEX_ARGS`** - additional arguments for codex
- **`CODEX_TIPS_PROMPT`** - show tmux tips prompt: `0` to disable, `1` to force (default respects a persisted opt-out)
 - **`CODEX_WATCH_MODE`** - `auto` (default), `tail`, or `multitail` to control codex-watch display

Example:
```bash
CODEX_CMD="cursor" CODEX_ARGS="--wait" codex-add /my/project
```

Flags:
- `codex-add -d`: start without attaching (useful in SSH automation)
- `codex-restore -a`: attach after restoring; `-f` to replace same-named windows

## Advanced Usage

### Board Session for Fast Navigation

Create a separate "board" session for quick navigation:

```bash
# Create board session
codex-board create

# Link all Codex windows to board
codex-board link

# Switch to board session
codex-board switch
```

Now you can use `tmux switch-client -t board` to scan through all Codex instances while the main `codexfarm` session continues running.

### Remote SSH Tips

- Start or restore your farm, then safely detach: `codex-restore; tmux detach`.
- Reattach anytime: `tmux attach -t ${CODEX_SESSION:-codexfarm}`.
- Prefer `codex-add -d` in automation to avoid stealing your current terminal.
- For mobile networks and roaming devices, use mosh: install `mosh` on the server (and open UDP 60000-61000), then connect with a mosh-capable client and attach your tmux session. Desktop SSH keeps working the same.
   - Example client wrapper (tries mosh then ssh): `examples/connect.sh user@host`
- If your terminal is very small (phones), use `codex-watch --simple` and zoom panes in tmux with `Prefix + z`.
 - Optional tmux tweak for mixed desktop/mobile: `tmux set -g aggressive-resize on` to let windows resize to the current client.

## Examples

- Start many projects at once (non-attaching):
  ```bash
  examples/batch-add.sh ~/proj/a ~/proj/b ~/proj/c
  tmux attach -t ${CODEX_SESSION:-codexfarm}
  ```


- Auto-restore on login (add to shell rc):
  ```bash
  # ~/.bashrc or ~/.zshrc
  source $(pwd)/examples/restore-on-login.sh
  ```

- One-liners:
  - Save then restore and attach: `codex-save && codex-restore -a`
  - Start with a different command: `CODEX_CMD="cursor" CODEX_ARGS="--wait" codex-add -d /path`
  - Watch logs with multitail if available: `codex-watch`

### Log Management

All logs are stored in `${XDG_STATE_HOME:-$HOME/.local/state}/codexfarm/logs/` with timestamps:

```bash
# View log status
codex-status logs

# Follow specific log
tail -f ~/.local/state/codexfarm/logs/myproject_20240315-143022.log

# Clean old logs (example: older than 7 days)
find ~/.local/state/codexfarm/logs -name "*.log" -mtime +7 -delete
```

## File Structure

```
codex-cli-farm/
├── setup.sh           # Main setup script
├── bin/               # Helper scripts
│   ├── codex-add      # Add new Codex instances
│   ├── codex-save     # Save manifest of windows
│   ├── codex-restore  # Restore windows from manifest
│   ├── codex-watch    # Monitor logs
│   ├── codex-board    # Navigation helper
│   └── codex-status   # Status information
├── examples/
│   ├── demo.sh        # End-to-end demo of farm
│   └── mock-codex     # Fake CLI used by the demo
├── validate.sh        # Basic repo validation script
└── README.md          # This file
```

## Requirements

- Linux or Unix-like system
- Bash shell
- One of: apt, dnf, yum, pacman, or zypper package managers
- Root access for package installation
  - If you don’t have sudo/root, install `tmux` and `multitail` manually and rerun `./setup.sh` to place scripts in `~/bin`.

## Limitations

- `tmux` cannot mirror the same live pane in two windows (use linked windows or logs)
- `pipe-pane` logs only new output after activation
 
- Manifest `cmd/args` are best-effort when saving from existing panes; windows created with `codex-add` restore reliably. Use env `CODEX_CMD/CODEX_ARGS` to override.

## License

Created on 2025-09-15T20:12:48Z
