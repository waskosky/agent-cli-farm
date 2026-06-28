# Codex CLI Farm

A tmux session manager for running and restoring multiple Codex CLI instances, with Claude and Gemini wrappers, logging, and monitoring built in.

## Features

- **Automated session management**: Long-lived tmux session that persists across reboots
- **Centralized logging**: Each Codex pane logs to individual files with timestamps
- **Unified monitoring**: Watch all Codex instances from a single consolidated view
- **Fast navigation**: Optional "board" session for quick switching between instances
- **Snapshot/restore**: Save a manifest of windows and restore them later
- **Status updates**: Tracks RUN/READY/ERR in tmux metadata and notifies when a window becomes READY
- **Memory warnings**: Flag tmux windows whose pane process trees exceed a chosen RSS threshold
- **Autosave/autorestore (optional)**: Systemd user services to persist sessions across logins
- **Prompt loopers**: Run one prompt or a prompt sequence repeatedly with retries, completion gates, git safety checks, logs, presets, and tmux visibility. See [Agent Looper Reference](docs/looper.md) for every parameter and default.
- **Tool wrappers**: `claude-*` and `gemini-*` commands use the same tmux workflow

## Requirements

- Bash 3.2+ and Python 3.10+.
- `tmux` for farm sessions, boards, status inspection, save/restore, and farm-launched loopers.
- The selected provider executable on `PATH` (`codex`, `claude`, `gemini`, or a custom command).
- Optional `multitail` for the richer `codex-watch` view. Without it, watch falls back to `tail`.
- Optional `lsof` so save/restore can find exact live provider session files.
- Git only for looper backup branches and git-progress circuit breakers.
- Systemd user services only for autosave/autorestore.

`setup.sh` reports missing operating-system dependencies; it does not install packages. Source it when you want the current shell's `PATH` refreshed. The script keeps strict shell options inside helper scopes, so sourcing it should not leak options or functions into your shell.

## Quick Start

### 1. One-time Setup

Run the setup script to install dependencies and create helper scripts (source it to auto-reload your shell):

```bash
source ./setup.sh
```

This will:
- Report missing `tmux` and `multitail` commands without running package-manager installs
- Create helper scripts in `$HOME/bin/`
- Set up logging directories
- Add `$HOME/bin` to your PATH automatically (bash/zsh/fish) and the current session

If `tmux` is unavailable, core tmux commands will not work until you install it. If `multitail` is unavailable, `codex-watch` falls back to a simpler `tail` view.

### 2. Add Codex, Claude, or Gemini Instances

From any project directory:
```bash
codex-add
```

Or specify a path:
```bash
codex-add /path/to/project
```

Use a named farm without exporting `CODEX_SESSION`:
```bash
codex-add work /path/to/project
codex-add work              # current directory in the "work" farm
```

Claude and Gemini use the same tmux workflow with wrappers:
```bash
claude-add /path/to/project
gemini-add /path/to/project
```

Launch notes:
- A single non-path positional value is treated as a farm name. Use `--session NAME` when that would be ambiguous.
- Provider flags after `--` are passed as argv data, which is the preferred path for one-off native options: `codex-add -d /repo -- --model gpt-5.4`.
- `CODEX_ARGS`, `CLAUDE_ARGS`, and `GEMINI_ARGS` remain trusted shell fragments for compatibility. Set them only from config you control.

### 3. Run Prompt Loopers

Create starter files in the project where an agent should work, then follow the printed next steps:
```bash
codex-looper
```

For guided setup:
```bash
codex-looper init --interactive --force
```

After editing `PROMPT.md`, start a looper in the default farm:
```bash
codex-looper
claude-looper
claude-looper -- --dangerously-skip-permissions
```

`claude-looper` defaults to a hybrid interface: the real Claude TTY stays visible in a tmux pane while the looper tracks session JSONL files for turn completion. Use `claude-looper --interface json` when you specifically need the older noninteractive stream-json mode.

For bounded smoke runs, legacy prompt sequences, completion-gated loops, or named farms:
```bash
codex-looper --once --label repo-smoke
codex-looper --mode sequence --prompt-file prompts.md --once
claude-looper --complete-on 'EXIT_SIGNAL:\s*true' --plan-file fix_plan.md --backup
codex-looper --cb-no-progress 3 --cb-output-decline 2 --backup
codex-looper --preset rai
claude-looper --farm-session work --label cleanup-pass --cwd /path/to/project
codex-looper --local --once --label local-smoke
```

Looper labels are used for logs and agent session names only. Farm tmux window names stay tied to `CODEX_NAME` or the working directory basename.
Farm-launched loopers use a two-pane tmux layout by default: the main pane shows supervisor status and the second pane tails the active prompt log with the live agent transcript. Use `--tmux-layout single` or `CODEX_LOOPER_LAYOUT=single` to keep one pane.

Inspect a running looper or agent without attaching:
```bash
codex-status activity
codex-status loopers
```

See [Agent Looper Reference](docs/looper.md) for prompt format, CLI parameters, config defaults, stop conditions, farm integration, and current backend limits.

Looper defaults and limits:
- The `run` subcommand is optional. In an initialized directory, `codex-looper` means `codex-looper run`.
- Prompt mode defaults to `single` with `PROMPT.md`. A custom `--prompt-file` also defaults to `single`; the legacy default file `prompts.md` implies `sequence`.
- Running loopers reload the prompt file before each loop by default, so prompt edits apply on the next pass. Set `reload_prompt_each_loop = false` only when a run must keep the startup prompt text.
- Config loading is strict: documented scalars must have the expected TOML type, numeric values must be finite, and invalid regexes fail before the loop starts.
- Backup branches point to committed `HEAD` only; they are not dirty-worktree snapshots. Pruning stays inside the exact configured prefix namespace.
- The no-progress circuit breaker fingerprints committed `HEAD`, status entries, tracked metadata, and file contents while ignoring the looper run directory.
- Run directories include a high-resolution timestamp and random suffix; the current-log pointer is updated atomically. In split mode, if tmux cannot create the tail pane, live transcript streaming falls back to the supervisor pane.
- Every looper run writes durable machine-readable state to `state.json` and append-only lifecycle history to `events.jsonl` in its run directory. `codex-status loopers` reads those files, so stop reasons survive pane exits and can be scraped without tmux.

### 4. Watch All Instances

Monitor all Codex logs in real-time:
```bash
codex-watch
```

Notes:
- On small terminals (phones), `codex-watch` auto-switches to a simpler mode.
- Force simple mode: `codex-watch --simple` or `CODEX_WATCH_MODE=tail codex-watch`.
- Force full mode: `codex-watch --mode multitail`.
- Logs contain output emitted after `tmux pipe-pane` is enabled; old scrollback cannot be recovered into log files.
- `codex-watch` discovers log files once at startup. Restart it to include newly-created logs.
- Multi-file tail output keeps source labels so interleaved lines can still be traced to a window log.
- First run shows an optional tmux tips prompt; choose Yes to see basics. Answer "Don't show again" to persist your preference. Re-enable temporarily with `CODEX_TIPS_PROMPT=1` or permanently by removing `~/.local/state/codexfarm/no_tips`.

### 5. Save/Restore or Resume

Snapshot your current Codex windows:
```bash
codex-save              # writes to ~/.config/codexfarm/manifest.tsv
CODEX_SESSION=work codex-save
# writes to ~/.config/codexfarm/manifests/work.tsv
codex-save --all-registered
# writes one manifest per farm registered for autoservice
```

Restore them later (e.g., after reboot or on SSH login):
```bash
codex-restore -a        # recreates and attaches to the session
```

Use `-f` to force re-creation of existing-named windows.
Saved Codex, Claude, and Gemini windows restore with exact session IDs when `codex-save` can read the live session file from the pane's process tree. If the exact session is unavailable, restore falls back by tool: `codex resume --last`, `claude --continue`, or `gemini --resume latest`.
Only pane 0 is saved. Split layouts and scrollback are not reconstructed. Missing saved directories fall back to `$HOME` with a warning.
Manifests are written owner-only and via atomic replacement, but they are still trusted executable input because restore launches the recorded commands.

If tmux sessions are already running (no manifest needed):
```bash
codex-resume            # joins the main Codex session if present
codex-resume work       # joins the named "work" farm
codex-resume work --board
```
Flags:
- `--board` to prefer the board session first.
- `--session NAME` to force a specific farm when a positional argument would be ambiguous.

### 6. (Optional) Enable Autosave/Autorestore

`codex-add` can install systemd user services to autosave hourly and restore on login.
You can trigger it directly:

```bash
codex-add --install-autoservice
```

The installed systemd unit names are always the same: `codex-autosave.service`, `codex-autosave.timer`, and `codex-autorestore.service`. Installing autoservice for a named farm adds that farm to `~/.config/codexfarm/farms.tsv`; it does not create a second background service:

```bash
codex-add --session work --install-autoservice
codex-add --session personal --install-autoservice
```

Autosave/autorestore iterates the registry, so each registered farm is saved to its own manifest and restored into its own tmux session.

Set `CODEX_AUTOSERVICE_CHOICE=yes` to auto-accept the prompt, or `no` to suppress it.

## Status Updates (RUN/READY/ERR)

`codex-add` auto-starts `codex-annotator`, which tracks RUN, READY, or ERR state in tmux window options. For Codex/Claude panes it inspects recent output for prompts/approval selections; other panes fall back to the command-based heuristic.

By default, the annotator does not rewrite tmux window titles. That lets Codex's native title animation remain visible while still making `codex-status windows` show state. When a window transitions from RUN to READY, the annotator emits a tmux `display-message` notification.

Important: the **RUN/READY/ERR status** is best-effort and based on terminal-output and command heuristics. Node-backed tools are recognized from pane start commands as well as current commands. Use READY as a signal, not a guarantee. Codex's native title animation is usually the primary visual signal.

### Memory Flags

Run `codex-memoryflag` to prefix high-memory tmux windows with a marker such as `*200+MB**`. It scans tmux sockets available to the current user, sums each window's pane process trees by RSS, and renames windows at or above the threshold. Existing memory markers are updated or cleared on each run, so window renaming is an intentional side effect.

```bash
codex-memoryflag        # flag windows at 200 MiB and up
codex-memoryflag 500    # flag windows at 500 MiB and up
codex-memoryflag 1G     # flag windows at 1024 MiB and up
codex-memoryflag -n     # dry run
```

The status annotator preserves memory markers if legacy title updates are enabled, so a title can read `*200+MB** *RUN* project-name`.

Tuning and controls:
- Disable autostart: `CODEX_ANNOTATOR_AUTOSTART=0`
- Disable annotator (if started): `CODEX_ANNOTATOR_ENABLED=0`
- Re-enable legacy title prefixes: `CODEX_ANNOTATOR_UPDATE_TITLES=1`
- Disable READY notifications: `CODEX_ANNOTATOR_NOTIFY_READY=0`
- Customize READY notification text: `CODEX_ANNOTATOR_READY_MESSAGE` (default: `READY: {name}`)
- Adjust RUN detection: `CODEX_ANNOTATOR_RUNNING_REGEX` (default: `(codex|node|ssh)`)
- Scope sessions: `CODEX_ANNOTATOR_SESSION_REGEX` (default: `^codex`)
- Ignore windows/sessions prefixed with `!` (configurable via `CODEX_ANNOTATOR_IGNORE_PREFIX`)
- Adjust capture depth: `CODEX_ANNOTATOR_CAPTURE_LINES` (default: `200`)

## Available Commands

### Core Commands

- **`codex-add [session] [directory]`** - Add a new Codex instance, optionally selecting a named farm
- **`codex-annotator`** - Track RUN/READY/ERR state and notify when windows become READY
- **`codex-memoryflag [threshold]`** - Flag high-memory tmux windows; default threshold is 200 MiB
- **`codex-watch`** - Monitor all Codex logs in consolidated view
- **`codex-looper [init|doctor|run]`** - Run a single prompt or prompt sequence repeatedly with logs and stop detection; see [looper reference](docs/looper.md)
- **`codex-status [--session SESSION] [sessions|windows|activity|logs|loopers]`** - Show status information
- **`codex-board [create|link|switch] [session]`** - Manage the default or a named board session for navigation
- **`codex-resume [session] [--board]`** - Attach/switch to an existing Codex/tmux session or named farm board
- **`codex-save [manifest]`** - Snapshot current windows to a manifest (TSV)
- **`codex-restore [-a] [-f] [manifest]`** - Restore windows from a manifest

### Claude and Gemini Wrappers

Claude and Gemini equivalents use the same tmux workflow and accept the same flags:
`claude-add`, `claude-annotator`, `claude-board`, `claude-looper`, `claude-restore`, `claude-resume`, `claude-save`, `claude-status`, `claude-watch`, `gemini-add`, `gemini-annotator`, `gemini-board`, `gemini-looper`, `gemini-restore`, `gemini-resume`, `gemini-save`, `gemini-status`, `gemini-watch`.

## Environment Variables

Common:
- **`CODEX_SESSION`** - tmux session name (default: `codexfarm`)
- **`CODEX_NAME`** - window name (default: directory basename)
- **`CODEX_CMD`** - command to run (default: `codex`)
- **`CODEX_ARGS`** - additional arguments for codex
- **`CODEX_STATE_BASENAME`** - state/log directory base name (default: `codexfarm`)
- **`CODEX_TIPS_PROMPT`** - show tmux tips prompt: `0` to disable, `1` to force (default respects a persisted opt-out)
- **`CODEX_LOCK_TITLES`** - set to `1` to keep Codex windows named after their directory (default `0` lets Codex's native title updates show)
- **`CODEX_REMAIN_ON_EXIT`** - keep tmux windows visible after the pane command exits (default `1`; set to `0` to close windows on exit)
- **`CODEX_WATCH_MODE`** - `auto` (default), `tail`, or `multitail` to control codex-watch display
- **`CODEX_STATUS_ACTIVITY_LINES`** - recent pane lines shown by `codex-status activity` (default `8`)
- **`CODEX_LOOPER_STATE_ROOT`** - run-state directory for `codex-status loopers` (default: `.agent-looper/runs` in the current directory)
- **`CODEX_AUTOSERVICE_CHOICE`** - `yes` or `no` to persist autoservice choice
- **`CODEX_ANNOTATOR_AUTOSTART`** - set to `0` to skip starting the annotator
- **`CODEX_LOOPER_PYTHON_BIN`** - Python 3.10+ interpreter for `codex-looper` (default searches `python3`, then versioned `python3.14` through `python3.10`)
- **`CODEX_ANNOTATOR_PYTHON_BIN`** - Python 3.10+ interpreter for `codex-annotator` (default searches `python3`, then versioned `python3.14` through `python3.10`)
- **`CODEXFARM_PYTHON_BIN`** - Python 3.10+ interpreter to prefer during `./setup.sh`
- **`CODEX_LOOPER_LAYOUT`** - looper tmux layout: `auto`, `single`, or `split`; farm launches default to `split`

Annotator-specific:
- **`CODEX_ANNOTATOR_ENABLED`** - set to `0` to disable the annotator loop
- **`CODEX_ANNOTATOR_UPDATE_TITLES`** - set to `1` to restore legacy `*RUN*`/`*READY*`/`*ERR*` title prefixes
- **`CODEX_ANNOTATOR_NOTIFY_READY`** - set to `0` to disable tmux messages when a window transitions from RUN to READY
- **`CODEX_ANNOTATOR_READY_MESSAGE`** - tmux message template for READY notifications; supports `{name}` and `{state}`
- **`CODEX_ANNOTATOR_RUNNING_REGEX`** - regex for pane commands considered RUNNING
- **`CODEX_ANNOTATOR_SESSION_REGEX`** - regex for sessions to annotate
- **`CODEX_ANNOTATOR_SESSION_REGISTRY`** - file of additional tmux session names to annotate (default: `${XDG_STATE_HOME:-$HOME/.local/state}/codexfarm/managed_sessions`)
- **`CODEX_ANNOTATOR_INTERVAL`** - polling interval in seconds
- **`CODEX_ANNOTATOR_IGNORE_PREFIX`** - window/session name prefix to ignore (default: `!`)
- **`CODEX_ANNOTATOR_CAPTURE_LINES`** - number of lines to capture from panes (default: `200`)

Tool-specific:
- `claude-add` and `gemini-add` honor `CLAUDE_*` or `GEMINI_*` versions of the common launch variables. For save/restore/resume/status/watch/board commands, select the farm with `CODEX_SESSION` or the command's positional/`--session` argument where supported.
- `codex-looper` and `claude-looper` pass native agent flags after `--`, for example `claude-looper --once -- --dangerously-skip-permissions`. Built-in Codex and Claude agents can also set `model` and `effort` in `[agents.*]` config. Claude uses `interface = "hybrid"` by default; set `--interface json` or `[agents.claude].interface = "json"` for the older stream-json path. The Claude flag is spelled `--dangerously-skip-permissions` and should only be used in isolated workspaces where unattended edits are acceptable.

Example:
```bash
CODEX_CMD="cursor" CODEX_ARGS="--wait" codex-add /my/project
```

Flags:
- `codex-add -d`: start without attaching (useful in SSH automation)
- `codex-restore -a`: attach after restoring; `-f` to replace same-named windows

## Advanced Usage

### Board Session for Fast Navigation

Create a separate board session for quick navigation. The default farm uses the legacy `board` session name; named farms use `<farm>-board`:

```bash
# Create board session for the default farm
codex-board create

# Create and use a named board
codex-board create work
codex-board link work
codex-board switch work

# Link all Codex windows to the default board
codex-board link

# Switch to the default board session
codex-board switch
```

Now you can use `tmux switch-client -t board` to scan through all Codex instances while the main `codexfarm` session continues running.
For named farms, `codex-resume work --board` jumps directly to `work-board`.
Boards link existing tmux windows; they do not duplicate provider processes.

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
  - Start in a named farm without env vars: `codex-add work /path`
  - Watch logs with multitail if available: `codex-watch`

### Log Management

All logs are stored in `${XDG_STATE_HOME:-$HOME/.local/state}/${CODEX_STATE_BASENAME:-codexfarm}/logs/` with timestamps:

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

For the static checks used in CI without creating live tmux sessions:

```bash
VALIDATE_SKIP_TMUX=1 ./validate.sh
```

## File Structure

```
codex-cli-farm/
├── .editorconfig      # Shared editor whitespace defaults
├── .github/workflows/ # CI for lint, shell checks, tests, validate, demo
├── pyproject.toml     # Ruff and test tooling configuration
├── requirements-dev.txt # Python development dependencies
├── CONTRIBUTING.md    # Local development and verification workflow
├── setup.sh           # Main setup script
├── bin/               # Helper scripts
│   ├── codex-add      # Add new Codex instances
│   ├── codex-annotator  # Bash wrapper for annotator
│   ├── codex-annotator.py # Track tmux window status and READY notifications
│   ├── codex-save     # Save manifest of windows
│   ├── codex-restore  # Restore windows from manifest
│   ├── codex-watch    # Monitor logs
│   ├── codex-board    # Navigation helper
│   ├── codex-resume   # Resume into existing session(s)
│   ├── codex-status   # Status information
│   └── claude-* / gemini-*  # Tool wrappers for the same commands
├── docs/
│   ├── looper.md      # Detailed looper contract
│   └── QUALITY_REVIEW.md # Hardening review notes from the update guide
├── examples/
│   ├── demo.sh        # End-to-end demo of farm
│   └── mock-codex     # Fake CLI used by the demo
├── tests/             # Python unit tests for scripts and looper behavior
├── validate.sh        # Basic repo validation script
└── README.md          # This file
```

## Limitations

- `tmux` cannot mirror the same live pane in two windows (use linked windows or logs)
- `tmux` survives client disconnects, but not host reboot or tmux-server exit unless autosave/autorestore recreates windows later.
- Autosave/autorestore is systemd-user-only.
- `pipe-pane` logs only new output after activation
- Manifest `cmd/args` are best-effort when saving from existing panes. Exact session IDs are captured from live processes when `lsof` can see the open session file: Codex under `~/.codex/sessions`, Claude under `~/.claude/projects`, and Gemini under a `.gemini/.../chats` directory. Missing IDs fall back to each tool's latest/continue behavior.
- A single positional argument to `codex-add` is interpreted as a farm name when it does not look like a path. Use `--session NAME` to force farm selection when needed.
- Gemini looper support is generic command execution, not a verified resumable protocol.

## License

Licensed under the MIT License. See `LICENSE` for full text.
Unless noted otherwise, all files in this repository are covered by the MIT License.
