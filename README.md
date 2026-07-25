# Agent CLI Farm

A tmux session manager for running and restoring Codex, Claude, Gemini, and custom coding-agent CLIs, with logging and monitoring built in.

This project was formerly named **Codex CLI Farm**. Existing `codex-*` commands,
`CODEX_*` environment variables, and `codexfarm` state paths remain supported for
backward compatibility.

## Features

- **Automated session management**: Long-lived tmux session that persists across reboots
- **Durable pane history**: Optional tmux-deep-history integration adds rotated raw/normalized transcripts, a seamless Page Up handoff beyond tmux's in-memory buffer, and compatible timestamped farm logs
- **Unified monitoring**: Watch all agent instances from a single consolidated view
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
- Optional network access during `setup.sh --with-deep-history` to download the checksum-pinned plugin release.

`setup.sh` reports missing operating-system dependencies; it does not install packages. Source it when you want the current shell's `PATH` refreshed. The script keeps strict shell options inside helper scopes, so sourcing it should not leak options or functions into your shell.

## Quick Start

### 1. One-time Setup

Run the setup script to create helper scripts, install the pinned deep-history integration, and
auto-reload your shell:

```bash
source ./setup.sh --with-deep-history
```

This will:
- Report missing `tmux` and `multitail` commands without running package-manager installs
- Create helper scripts in `$HOME/bin/`
- Set up logging directories
- Add `$HOME/bin` to your PATH automatically (bash/zsh/fish) and the current session
- Verify and install the pinned `tmux-deep-history` release under `$XDG_DATA_HOME/codexfarm/plugins/` (or `~/.local/share/codexfarm/plugins/`)

If `tmux` is unavailable, core tmux commands will not work until you install it. If `multitail` is unavailable, `codex-watch` falls back to a simpler `tail` view.
Omit `--with-deep-history` when you want the legacy flat-log backend only. Re-running the setup
command is safe; the installed version can change only when this repository's reviewed lock changes.

Deep history is intentionally opt-in because terminal transcripts can contain commands, private
paths, credentials, and other sensitive output. Once installed, the default `auto` backend starts
recording current tmux panes and enables the plugin's global tmux hooks for panes created afterward.
Set `CODEXFARM_HISTORY_BACKEND=legacy` when launching a pane to turn those hooks off and use only
the flat compatibility log. If the plugin is absent, `codex-add` reports the legacy fallback
instead of silently making deep history look active.

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

`codex-looper` and `claude-looper` default to a hybrid interface: the real agent TTY stays visible in a tmux pane while the looper tracks session JSONL files for turn completion. Use `--interface json` when you specifically need the older noninteractive JSON stream mode.

For bounded smoke runs, legacy prompt sequences, completion-gated loops, or named farms:
```bash
codex-looper --once --label repo-smoke
codex-looper --mode sequence --prompt-file prompts.md --once
claude-looper --complete-on 'EXIT_SIGNAL:\s*true' --plan-file fix_plan.md --backup
codex-looper --cb-no-progress 3 --cb-output-decline 2 --backup
codex-looper --cb-output-match 'STATUS:\s*BLOCKED' --cb-output-match-repeats 3
codex-looper --preset rai
claude-looper --farm-session work --label cleanup-pass --cwd /path/to/project
codex-looper --local --once --label local-smoke
```

Looper labels are used for logs and agent session names only. Farm tmux window names stay tied to `CODEX_NAME` or the working directory basename.
Farm-launched loopers use a two-pane tmux layout by default. Claude hybrid runs use the main pane as the merged supervisor/status/control surface and the second pane for the live Claude Code TTY. Non-hybrid split runs use the second pane as a looper control pane with the live agent transcript. Use `--tmux-layout single` or `CODEX_LOOPER_LAYOUT=single` to keep one pane.

Inspect a running looper or agent without attaching:
```bash
codex-status activity
codex-status loopers
```

Queue a safe stop without attaching:
```bash
codex-looper control stop LOOPER-rai --after-loop --reason "merge checkpoint"
```

Record the agent's current high-level focus without attaching:
```bash
codex-looper control focus LOOPER-rai --summary "Verifying that live screenshots are still reaching the validation loop."
```

Send or record an operator note without attaching:
```bash
codex-looper control note LOOPER-rai --delivery btw --note "controller drift was fixed after the latest calibration"
codex-looper control note LOOPER-rai --delivery record --note-file ./handoff-note.md
```

In the split looper control pane, enter `b NOTE` to send `NOTE` through Claude's
`/btw` side channel to the active hybrid pane. Notes are also recorded in the
run directory as `operator_notes.jsonl`. When run state does not include a
hybrid pane id, pass an explicit `--pane`; broad tmux pane scanning requires
`--allow-pane-scan` after verifying the intended recipient.

Agents can refresh the visible supervisor focus line from inside a run with
`codex-looper control focus --run-dir "$CODEX_LOOPER_RUN_DIR" --summary "..."`.
Keep it to one human-readable sentence about the larger stroke of work; the
append-only history is stored in `focus.jsonl`.

See [Agent Looper Reference](docs/looper.md) for prompt format, CLI parameters, config defaults, stop conditions, farm integration, and current backend limits.

Looper defaults and limits:
- The `run` subcommand is optional. In an initialized directory, `codex-looper` means `codex-looper run`.
- Prompt mode defaults to `single` with `PROMPT.md`. A custom `--prompt-file` also defaults to `single`; the legacy default file `prompts.md` implies `sequence`.
- Running loopers reload the prompt file before each loop by default, so prompt edits apply on the next pass. Set `reload_prompt_each_loop = false` only when a run must keep the startup prompt text.
- Config loading is strict: documented scalars must have the expected TOML type, numeric values must be finite, and invalid regexes fail before the loop starts.
- Backup branches point to committed `HEAD` only; they are not dirty-worktree snapshots. Pruning stays inside the exact configured prefix namespace.
- The no-progress circuit breaker fingerprints committed `HEAD`, status entries, tracked metadata, and file contents while ignoring the looper run directory.
- The output-match circuit breaker stops repeated project-defined status reports, for example a loop that keeps saying it is blocked.
- Run directories include a high-resolution timestamp and random suffix; the current-log pointer is updated atomically. In split mode, if tmux cannot create the control pane, live transcript streaming falls back to the supervisor pane.
- Every looper run writes durable machine-readable state to `state.json`, append-only lifecycle history to `events.jsonl`, visible focus history to `focus.jsonl`, and accepts optional operator commands in `control.jsonl` in its run directory. `codex-status loopers` reads state files, flags active states whose supervisor process is gone or defunct as stale, and `codex-status loopers --repair-stale-loopers` records those states as externally stopped so stop reasons survive pane exits and can be scraped without tmux.
- `codex-looper control stop --now` interrupts all known runtime targets, including hybrid tmux pane descendants and process groups. Use `codex-looper control stop LABEL --force` for a stuck looper that needs SIGTERM/SIGKILL escalation and stale-state repair.

### 4. Watch All Instances

Monitor all Codex logs in real-time:
```bash
codex-watch
```

Notes:
- On small terminals (phones), `codex-watch` auto-switches to a simpler mode.
- Force simple mode: `codex-watch --simple` or `CODEX_WATCH_MODE=tail codex-watch`.
- Force full mode: `codex-watch --mode multitail`.
- With the installed deep-history backend, one `pipe-pane` owner writes durable segmented history and mirrors the live raw stream into the same flat logs used by `codex-watch`.
- Flat compatibility logs contain output emitted after the pane pipe is enabled. Deep history separately captures scrollback already visible when recording starts.
- Deep history is not a literal extension of tmux's internal grid. Use `Prefix + [` for recent copy-mode history; when Page Up reaches the absolute top, the farm's default seamless handoff opens the older disk transcript in a popup. `Prefix + H` opens the full Deep History menu, and `Alt-u` opens older history directly from copy mode. Mouse-wheel scrolling remains inside tmux's native buffer and does not cross the disk-history boundary.
- Plugin installation raises tmux's global history limit from its usual 2,000 rows to 50,000 for panes created afterward. Existing panes keep their original in-memory limit, but `codex-add` backfills deep-history recording for existing tmux panes unless another output pipe already owns one. Recreate panes (or save and reboot the farm) only when you also want the larger in-memory limit.
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

Reboot a running farm through the complete save, stop, and restore sequence:

```bash
codex-farm-reboot              # reboot the default farm and attach
codex-farm-reboot --detach     # restore without attaching
codex-farm-reboot work         # reboot the named "work" farm
claude-farm-reboot --detach    # use Claude defaults for fallback restore commands
gemini-farm-reboot --detach    # use Gemini defaults for fallback restore commands
```

The reboot command saves before stopping anything, stops a linked board before the main
farm, restores the farm, and recreates the board only when it existed beforehand. Run it
from a separate terminal or a different tmux session; it refuses to kill the session or
board that is currently hosting the command. Checkpoint active agent work first because
exact provider-session discovery is best-effort.

Use `-f` to force re-creation of existing-named windows.
Saved Codex, Claude, and Gemini windows restore with exact session IDs when `codex-save` can read the live session file from the pane's process tree. If the exact session is unavailable, restore falls back by tool: `codex resume --last`, `claude --continue`, or `gemini --resume latest`.
Only pane 0 is saved. Split layouts and scrollback are not reconstructed. Missing saved directories fall back to `$HOME` with a warning.
Manifests are written owner-only and via atomic replacement, but they are still trusted executable input because restore launches the recorded commands.

Check installed-helper freshness and manifest resume coverage without printing session IDs:

```bash
codex-doctor
CODEX_SESSION=work codex-doctor
codex-doctor --source /path/to/agent-cli-farm /path/to/manifest.tsv
```

The doctor exits nonzero for stale or missing installed helpers, malformed or unsafe manifests, blank commands, and provider fallbacks that could resume the wrong conversation.

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
- **`codex-status [--session SESSION] [sessions|windows|activity|logs|loopers]`** - Show status information; `loopers --repair-stale-loopers` marks active state files stopped when their supervisor process is gone
- **`codex-board [create|link|switch] [session]`** - Manage the default or a named board session for navigation
- **`codex-resume [session] [--board]`** - Attach/switch to an existing Codex/tmux session or named farm board
- **`codex-doctor [--session NAME] [--source DIR] [manifest]`** - Check installed-helper freshness and manifest resume coverage without displaying session IDs
- **`codex-save [manifest]`** - Snapshot current windows to a manifest (TSV)
- **`codex-restore [-a] [-f] [manifest]`** - Restore windows from a manifest
- **`codex-farm-reboot [--detach] [session]`** - Safely save, stop, restore, and optionally attach to a farm

### Claude and Gemini Wrappers

Claude and Gemini equivalents use the same tmux workflow and accept the same flags:
`claude-add`, `claude-annotator`, `claude-board`, `claude-farm-reboot`, `claude-looper`, `claude-restore`, `claude-resume`, `claude-save`, `claude-status`, `claude-watch`, `gemini-add`, `gemini-annotator`, `gemini-board`, `gemini-farm-reboot`, `gemini-looper`, `gemini-restore`, `gemini-resume`, `gemini-save`, `gemini-status`, `gemini-watch`.

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
- **`CODEXFARM_WITH_DEEP_HISTORY`** - set to `1` as an alternative to `setup.sh --with-deep-history`
- **`CODEXFARM_HISTORY_BACKEND`** - `auto` (default), `legacy`, or `deep-history`; auto uses the pinned plugin when installed and otherwise preserves legacy logging
- **`CODEXFARM_DEEP_HISTORY_BIN`** - override the deep-history executable discovered under the XDG data directory
- **`CODEXFARM_DEEP_HISTORY_PYTHON_BIN`** - override the Python 3.10+ interpreter used by deep-history hooks and loggers (default searches supported `python3` and versioned commands)
- **`CODEXFARM_DEEP_HISTORY_SEAMLESS_PAGEUP`** - set to `0` to keep tmux's ordinary Page Up behavior at the top of copy mode instead of opening the older disk transcript (default `1`)
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
- `codex-looper` and `claude-looper` pass native agent flags after `--`, for example `codex-looper --once -- --ask-for-approval never` or `claude-looper --once -- --dangerously-skip-permissions`. Built-in Codex and Claude agents can also set `model` and `effort` in `[agents.*]` config. Codex and Claude use `interface = "hybrid"` by default; set `--interface json`, `[agents.codex].interface = "json"`, or `[agents.claude].interface = "json"` for the older JSON stream path. The Claude flag is spelled `--dangerously-skip-permissions` and should only be used in isolated workspaces where unattended edits are acceptable.

Example:
```bash
CODEX_CMD="cursor" CODEX_ARGS="--wait" codex-add /my/project
```

Flags:
- `codex-add -d`: start without attaching (useful in SSH automation)
- `codex-restore -a`: attach after restoring; `-f` to replace same-named windows
- `codex-farm-reboot -d`: save, stop, and restore without attaching

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
  - Safely reboot and attach: `codex-farm-reboot`
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
./tests/integration/session_resume_smoke.sh
CODEXFARM_DEEP_HISTORY_BIN=/path/to/tmux-deep-history/bin/tmux-deep-history \
  ./tests/integration/deep_history_smoke.sh
```

For the static checks used in CI without creating live tmux sessions:

```bash
VALIDATE_SKIP_TMUX=1 ./validate.sh
```

## File Structure

```
agent-cli-farm/
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
│   ├── codex-doctor   # Diagnose installed-helper and manifest drift
│   ├── codex-save     # Save manifest of windows
│   ├── codex-restore  # Restore windows from manifest
│   ├── codex-farm-reboot # Save, stop, and restore a farm
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
├── integrations/      # Pinned deep-history lock and atomic installer
├── tests/             # Python unit tests for scripts and looper behavior
├── validate.sh        # Basic repo validation script
└── README.md          # This file
```

## Limitations

- `tmux` cannot mirror the same live pane in two windows (use linked windows or logs)
- `tmux` survives client disconnects, but not host reboot or tmux-server exit unless autosave/autorestore recreates windows later.
- Autosave/autorestore is systemd-user-only.
- tmux provides one `pipe-pane` consumer per pane; the deep-history backend owns it and mirrors the stream rather than attaching a competing logger.
- Manifest `cmd/args` are best-effort when saving from existing panes. Exact session IDs are captured from live processes when `lsof` can see the open session file: Codex under `~/.codex/sessions`, Claude under `~/.claude/projects`, and Gemini under a `.gemini/.../chats` directory. Missing IDs fall back to each tool's latest/continue behavior.
- A single positional argument to `codex-add` is interpreted as a farm name when it does not look like a path. Use `--session NAME` to force farm selection when needed.
- Gemini looper support is generic command execution, not a verified resumable protocol.

## License

Licensed under the MIT License. See `LICENSE` for full text.
Unless noted otherwise, all files in this repository are covered by the MIT License.
