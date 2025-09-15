# Codex CLI Farm

A comprehensive tmux session management system for easily managing and restoring tmux sessions that auto-consolidate and run all your Codex CLI instances.

## Features

- **Automated session management**: Long-lived tmux session that persists across reboots
- **Process adoption**: Reparent already-running Codex instances into tmux with reptyr
- **Centralized logging**: Each Codex pane logs to individual files with timestamps
- **Unified monitoring**: Watch all Codex instances from a single consolidated view
- **Fast navigation**: Optional "board" session for quick switching between instances
- **Cross-platform**: Works with all major Linux package managers

## Quick Start

### 1. One-time Setup

Run the setup script to install dependencies and create helper scripts:

```bash
./setup.sh
```

This will:
- Install `tmux`, `reptyr`, and `multitail` using your system's package manager
- Create helper scripts in `$HOME/bin/`
- Set up logging directories

Make sure `$HOME/bin` is in your PATH:
```bash
export PATH="$HOME/bin:$PATH"
# Add to your ~/.bashrc or ~/.zshrc to make permanent
```

### 2. Add Codex Instances

From any project directory:
```bash
codex-add
```

Or specify a path:
```bash
codex-add /path/to/project
```

### 3. Watch All Instances

Monitor all Codex logs in real-time:
```bash
codex-watch
```

### 4. Adopt Existing Processes

If you have Codex already running outside tmux:

1. Find the process ID:
   ```bash
   pgrep -fa codex
   ```

2. Adopt it into tmux:
   ```bash
   codex-adopt 12345
   ```

## Available Commands

### Core Commands

- **`codex-add [directory]`** - Add new Codex instance to tmux session
- **`codex-adopt PID`** - Adopt existing Codex process with reptyr
- **`codex-watch`** - Monitor all Codex logs in consolidated view
- **`codex-status [sessions|windows|logs]`** - Show status information
- **`codex-board [create|link|switch]`** - Manage board session for navigation

### Environment Variables

You can customize behavior with these environment variables:

- **`CODEX_SESSION`** - tmux session name (default: `codexfarm`)
- **`CODEX_NAME`** - window name (default: directory basename)
- **`CODEX_CMD`** - command to run (default: `codex`)
- **`CODEX_ARGS`** - additional arguments for codex

Example:
```bash
CODEX_CMD="cursor" CODEX_ARGS="--wait" codex-add /my/project
```

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

### Troubleshooting reptyr

If `codex-adopt` shows "Operation not permitted":

1. **Try TTY-stealing mode** (recommended):
   ```bash
   sudo reptyr -T -s PID
   ```

2. **Or temporarily allow ptrace**:
   ```bash
   sudo sysctl -w kernel.yama.ptrace_scope=0
   codex-adopt PID
   sudo sysctl -w kernel.yama.ptrace_scope=1  # revert
   ```

Some processes may need a screen redraw after adoption (Ctrl+L).

## File Structure

```
codex-cli-farm/
├── setup.sh           # Main setup script
├── bin/               # Helper scripts
│   ├── codex-add      # Add new Codex instances
│   ├── codex-adopt    # Adopt existing processes
│   ├── codex-watch    # Monitor logs
│   ├── codex-board    # Navigation helper
│   └── codex-status   # Status information
└── README.md          # This file
```

## Requirements

- Linux or Unix-like system
- Bash shell
- One of: apt, dnf, yum, pacman, or zypper package managers
- Root access for package installation

## Limitations

- `tmux` cannot mirror the same live pane in two windows (use linked windows or logs)
- `pipe-pane` logs only new output after activation
- `reptyr` may require elevated privileges depending on system configuration
- Some TUI applications may need screen redraw after reptyr adoption

## License

Created on 2025-09-15T20:12:48Z
