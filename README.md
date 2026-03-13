# Codex CLI Farm

A tmux session manager for running and restoring multiple Codex CLI instances (plus optional Claude wrappers), with logging and monitoring built in.

## Features

- **Automated session management**: Long-lived tmux session that persists across reboots
- **Centralized logging**: Each Codex pane logs to individual files with timestamps
- **Unified monitoring**: Watch all Codex instances from a single consolidated view
- **Fast navigation**: Optional "board" session for quick switching between instances
- **Snapshot/restore**: Save a manifest of windows and restore them later
- **Status annotations**: *RUN*/*READY*/*ERR* prefixes in tmux window titles (Codex/Claude READY uses prompt parsing)
- **Autosave/autorestore (optional)**: Systemd user services to persist sessions across logins
- **Claude-friendly**: `claude-*` wrappers use the same tmux workflow

## Quick Start

### 1. One-time Setup

Run the setup script to install dependencies and create helper scripts (source it to auto-reload your shell):

```bash
source ./setup.sh
```

This will:
- Install missing `tmux` and `multitail` packages when needed
- Create helper scripts in `$HOME/bin/`
- Set up logging directories
- Add `$HOME/bin` to your PATH automatically (bash/zsh/fish) and the current session

If `tmux` and `multitail` are already on your `PATH`, the setup script skips package-manager work. If `multitail` is unavailable, `codex-watch` falls back to a simpler `tail` view.

### 2. Add Codex (or Claude) Instances

From any project directory:
```bash
codex-add
```

Or specify a path:
```bash
codex-add /path/to/project
```

Claude uses the same session with wrappers:
```bash
claude-add /path/to/project
```

### 3. Watch All Instances

Monitor all Codex logs in real-time:
```bash
codex-watch
```

Notes:
- On small terminals (phones), `codex-watch` auto-switches to a simpler mode.
- Force simple mode: `codex-watch --simple` or `CODEX_WATCH_MODE=tail codex-watch`.
- Force full mode: `codex-watch --mode multitail`.
- First run shows an optional tmux tips prompt; choose Yes to see basics. Answer "Don't show again" to persist your preference. Re-enable temporarily with `CODEX_TIPS_PROMPT=1` or permanently by removing `~/.local/state/codexfarm/no_tips`.

### 4. Save/Restore or Resume

Snapshot your current Codex windows:
```bash
codex-save              # writes to ~/.config/codexfarm/manifest.tsv
```

Restore them later (e.g., after reboot or on SSH login):
```bash
codex-restore -a        # recreates and attaches to the session
```

Use `-f` to force re-creation of existing-named windows.

If tmux sessions are already running (no manifest needed):
```bash
codex-resume            # joins the main Codex session if present
```
Flags:
- `--board` to prefer the board session first.

### 5. (Optional) Enable Autosave/Autorestore

`codex-add` can install systemd user services to autosave hourly and restore on login.
You can trigger it directly:

```bash
codex-add --install-autoservice
```

Set `CODEX_AUTOSERVICE_CHOICE=yes` to auto-accept the prompt, or `no` to suppress it.

## Status Annotations (RUN/READY/ERR)

`codex-add` auto-starts `codex-annotator`, which prefixes tmux window titles with *RUN*, *READY*, or *ERR*. For Codex/Claude panes it inspects recent output for prompts/approval selections; other panes fall back to the command-based heuristic.

Important: the **READY status** is best-effort and based on prompt detection. Use it as a signal, not a guarantee.

Tuning and controls:
- Disable autostart: `CODEX_ANNOTATOR_AUTOSTART=0`
- Disable annotator (if started): `CODEX_ANNOTATOR_ENABLED=0`
- Adjust RUN detection: `CODEX_ANNOTATOR_RUNNING_REGEX` (default: `(codex|node|ssh)`)
- Scope sessions: `CODEX_ANNOTATOR_SESSION_REGEX` (default: `^codex`)
- Ignore windows/sessions prefixed with `!` (configurable via `CODEX_ANNOTATOR_IGNORE_PREFIX`)
- Adjust capture depth: `CODEX_ANNOTATOR_CAPTURE_LINES` (default: `200`)

## Available Commands

### Core Commands

- **`codex-add [directory]`** - Add new Codex instance to tmux session
- **`codex-annotator`** - Annotate tmux window titles with RUN/READY/ERR status
- **`codex-watch`** - Monitor all Codex logs in consolidated view
- **`codex-status [sessions|windows|logs]`** - Show status information
- **`codex-board [create|link|switch]`** - Manage board session for navigation
- **`codex-resume [--board]`** - Attach/switch to an existing Codex/tmux session
- **`codex-save [manifest]`** - Snapshot current windows to a manifest (TSV)
- **`codex-restore [-a] [-f] [manifest]`** - Restore windows from a manifest

### Claude Wrappers

Claude equivalents use the same tmux workflow and accept the same flags:
`claude-add`, `claude-annotator`, `claude-board`, `claude-restore`, `claude-resume`, `claude-save`, `claude-status`, `claude-watch`.

## Environment Variables

Common:
- **`CODEX_SESSION`** - tmux session name (default: `codexfarm`)
- **`CODEX_NAME`** - window name (default: directory basename)
- **`CODEX_CMD`** - command to run (default: `codex`)
- **`CODEX_ARGS`** - additional arguments for codex
- **`CODEX_STATE_BASENAME`** - state/log directory base name (default: `codexfarm`)
- **`CODEX_TIPS_PROMPT`** - show tmux tips prompt: `0` to disable, `1` to force (default respects a persisted opt-out)
- **`CODEX_LOCK_TITLES`** - set to `0` to let tmux or shell rename windows automatically (default keeps Codex windows named after their directory)
- **`CODEX_WATCH_MODE`** - `auto` (default), `tail`, or `multitail` to control codex-watch display
- **`CODEX_AUTOSERVICE_CHOICE`** - `yes` or `no` to persist autoservice choice
- **`CODEX_ANNOTATOR_AUTOSTART`** - set to `0` to skip starting the annotator

Annotator-specific:
- **`CODEX_ANNOTATOR_ENABLED`** - set to `0` to disable the annotator loop
- **`CODEX_ANNOTATOR_RUNNING_REGEX`** - regex for pane commands considered RUNNING
- **`CODEX_ANNOTATOR_SESSION_REGEX`** - regex for sessions to annotate
- **`CODEX_ANNOTATOR_INTERVAL`** - polling interval in seconds
- **`CODEX_ANNOTATOR_IGNORE_PREFIX`** - window/session name prefix to ignore (default: `!`)
- **`CODEX_ANNOTATOR_CAPTURE_LINES`** - number of lines to capture from panes (default: `200`)

Claude-specific:
- Use `CLAUDE_*` versions of the common variables to override just the Claude wrappers.

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
- Reattach anytime: `codex-resume` (or `tmux attach -t ${CODEX_SESSION:-codexfarm}`).
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

## Validation

Run the basic validation script (requires tmux):

```bash
./validate.sh
```

## File Structure

```
codex-cli-farm/
├── setup.sh           # Main setup script
├── bin/               # Helper scripts
│   ├── codex-add      # Add new Codex instances
│   ├── codex-annotator  # Bash wrapper for annotator
│   ├── codex-annotator.py # Annotate tmux window titles (python)
│   ├── codex-save     # Save manifest of windows
│   ├── codex-restore  # Restore windows from manifest
│   ├── codex-watch    # Monitor logs
│   ├── codex-board    # Navigation helper
│   ├── codex-resume   # Resume into existing session(s)
│   ├── codex-status   # Status information
│   └── claude-*       # Claude wrappers for the same commands
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
  - If you don't have sudo/root, install `tmux` manually and rerun `./setup.sh` to place scripts in `~/bin`.
  - `multitail` is optional; without it, `codex-watch` uses simple mode automatically.

## Limitations

- `tmux` cannot mirror the same live pane in two windows (use linked windows or logs)
- `pipe-pane` logs only new output after activation
- Manifest `cmd/args` are best-effort when saving from existing panes; windows created with `codex-add` restore reliably. Use env `CODEX_CMD/CODEX_ARGS` to override.

## License

Licensed under the MIT License. See `LICENSE` for full text.
Unless noted otherwise, all files in this repository are covered by the MIT License.
