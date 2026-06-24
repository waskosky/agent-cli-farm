# Agent Looper Reference

`codex-looper`, `claude-looper`, and `gemini-looper` run repeated file-backed prompts against local agent CLIs. The default model is one prompt file looped forever with a fresh agent session each loop. Sequence mode, completion detection, task-plan gates, git backups, circuit breakers, and presets are available when a project needs them.

Runtime requirement: Python 3.10 or newer. On Python 3.11+, the standard library TOML parser is used; on Python 3.10, the looper uses its built-in parser for the supported `agent-looper.toml` schema documented below.

## First Run

From the repository where the agent should work:

```bash
codex-looper
```

If neither `agent-looper.toml` nor a prompt file exists, this writes starter `agent-looper.toml` and `PROMPT.md` files and prints next steps. Existing files are left unchanged.

With only `PROMPT.md` present, `codex-looper` runs in `single` mode without requiring a config file. With only legacy `prompts.md` present, it infers `sequence` mode for backward compatibility.

For guided setup:

```bash
codex-looper init --interactive --force
```

Interactive setup writes `PROMPT.md` in `single` mode when you enter one prompt. If you enter multiple prompts, it writes `prompts.md` and sets `mode = "sequence"`.

## Prompt Modes

`single` mode is the default. The whole prompt file is sent as one prompt each loop:

```text
Inspect this repository and implement the highest-value safe cleanup.
End every reply with EXIT_SIGNAL: false unless the work is complete.
```

`sequence` mode preserves the original behavior. It splits the prompt file on lines containing only `---`:

```toml
[looper]
mode = "sequence"
prompt_file = "prompts.md"
```

```text
Summarize the repository layout. Do not modify files.
---
Identify one safe cleanup. Do not modify files.
---
If the cleanup is obvious, implement it and run a fast check.
```

Prompts in one sequence share the same agent session. After a full sequence completes, the default behavior is to start a fresh session for the next loop.

## Common Commands

```bash
codex-looper
claude-looper
codex-looper --once --label smoke
codex-looper --mode sequence --prompt-file prompts.md --once
claude-looper --complete-on 'EXIT_SIGNAL:\s*true' --plan-file fix_plan.md --backup
codex-looper --cb-no-progress 3 --cb-output-decline 2 --backup --backup-keep 20
codex-looper --preset rai
codex-looper --once --label gpt5-high -- --model gpt-5.4 --effort high
claude-looper --once --label smoke -- --dangerously-skip-permissions
codex-looper --farm-session work --label cleanup --cwd /path/to/project
codex-looper --local --once --label local-preview
codex-looper --dry-run --once --label preview
```

Use `--once` or `--max-loops N` for bounded runs. Without either, the looper repeats until a stop condition occurs. Non-dry-run commands launch through the default farm unless `--local` is set.

`--timeout` is a per-prompt wall-clock limit for the local agent process. The default is 7200 seconds. Short values such as `90` seconds can stop a long Claude/Codex tool call even when the provider is still working; raise it on the command line or in `[looper].timeout_seconds` for heavier prompts.

## CLI Options

| Option | Default | Purpose |
| --- | --- | --- |
| `--agent NAME` | `[looper].default_agent` or wrapper default | Selects an agent config from `agent-looper.toml`. |
| `--config PATH` | `agent-looper.toml` | Project config file path. Missing config is allowed. |
| `--preset NAME_OR_PATH` | unset | Layer a preset TOML file over project config. Named presets resolve from `~/.config/codexfarm/presets/` and repo `examples/presets/`. |
| `--mode single\|sequence` | `single`, with legacy inference | Select prompt loading mode. |
| `--prompt-file PATH` | `PROMPT.md` in single mode, `prompts.md` in sequence mode | Prompt file. |
| `--label LABEL` | `Looper_<short-id>` | Human-readable run/session/log label. It does not rename tmux windows. |
| `--timeout SECONDS` | `7200` | Per-prompt subprocess timeout. |
| `--sleep SECONDS` | `2` | Sleep between completed loops. |
| `--max-loops N` | `0` | Maximum loops; `0` means unlimited. |
| `--max-transient-retries N` | `12` | Cap non-rate-limit transient retries; `0` means unlimited. |
| `--retry-notify-after SECONDS` | `300` | Show a tmux notification for retry waits at or above this threshold; `0` disables. |
| `--complete-on REGEX` | unset | Enable completion detection and stop after completed loops whose output matches the regex. |
| `--completion-streak N` | `1` | Require N consecutive completed loops with the completion marker before stopping. |
| `--plan-file PATH` | unset | Markdown checklist gate; completion requires no unchecked `- [ ]` tasks. |
| `--backup` | off | Create a git backup branch before each non-dry-run loop. |
| `--backup-prefix PREFIX` | `looper-backup` | Branch prefix for backup branches. |
| `--backup-keep N` | `10` | Prune older backup branches, keeping the newest N. `0` disables pruning. |
| `--cb-no-progress N` | `0` | Stop after N completed loops with no git HEAD or worktree-status change. |
| `--cb-output-decline N` | `0` | Stop after N consecutive completed loops whose captured output byte count is lower than the prior loop. |
| `--once` | off | Equivalent to `--max-loops 1`. |
| `--fresh-session-per-loop` | on | Start a new agent session for each completed loop. |
| `--reuse-session` | off | Reuse one agent session across all loops. |
| `--cwd PATH` | current directory | Working directory for agent commands. |
| `--dry-run` | off | Print commands without running agents or creating backup branches. |
| `--ignore-nonzero` | off | Continue after nonzero agent exits. |
| `--stop-on-nonzero` | on | Stop after nonzero agent exits. |
| `--hold-on-stop` | off | Wait for Enter before closing after stop. |
| `--local` | off | Run in the current terminal instead of launching through the default farm. |
| `--farm-session [NAME]` | default farm | Select the farm tmux session for `codex-add`. Omitting NAME uses the default farm session. |
| `--farm-attach` | off | Attach after `--farm-session` launch. |
| `--farm-add-bin PATH` | `codex-add` | Launcher compatible with `codex-add`. |
| `-- AGENT_ARGS...` | unset | Pass native flags to built-in Codex/Claude command templates. |

Use `--` for one-off agent-native flags. For Claude, the correct spelling is `--dangerously-skip-permissions`; local Claude help recommends it only for isolated sandboxes. To make it permanent for a project, put the same values in `[agents.claude].extra_args`.

## Config Defaults

Starter `agent-looper.toml` defaults:

```toml
[looper]
default_agent = "codex"
mode = "single"
prompt_file = "PROMPT.md"
timeout_seconds = 7200
sleep_seconds = 2
fresh_session_per_loop = true
max_loops = 0
max_transient_retries = 12
retry_notify_after_seconds = 300
log_dir = ".agent-looper/runs"
completion_enabled = false
completion_marker = "EXIT_SIGNAL:\\s*true"
completion_streak = 1
plan_file = ""
backup_enabled = false
backup_prefix = "looper-backup"
backup_keep = 10
cb_no_progress = 0
cb_output_decline = 0
```

Agent defaults:

| Agent | Command behavior |
| --- | --- |
| `codex` | First prompt: `codex exec --json <prompt>`; later prompts resume by Codex thread ID when available, otherwise `resume --last`. |
| `claude` | First prompt: `claude -p --output-format stream-json --verbose --name <session> <prompt>`; later prompts use `--resume <session>`. |
| `gemini` | Generic default: `gemini -p <prompt>` for every prompt. Override this if your Gemini CLI supports a better noninteractive/resume mode. |

Built-in Codex and Claude agents accept `model` and `effort` config sugar. These fields are appended after `extra_args` as `--model <value>` and `--effort <value>`:

```toml
[agents.codex]
kind = "codex"
model = "gpt-5.4"
effort = "high"
extra_args = ["--sandbox", "workspace-write"]

[agents.claude]
kind = "claude"
model = "claude-opus-4-8"
effort = "max"
```

Custom `first_command` and `resume_command` templates fully control their argv, so include model or effort flags directly in those templates when using a custom command.

Custom agents can use command templates:

```toml
[agents.my_agent]
kind = "generic"
first_command = ["my-agent", "run", "--session", "{session}", "{prompt}"]
resume_command = ["my-agent", "run", "--resume", "{session}", "{prompt}"]
scan_stdout_for_stop_patterns = true
```

Available placeholders: `{prompt}`, `{session}`, `{session_id}`, `{loop}`, `{prompt_index}`, `{label}`, `{run_dir}`.

## Completion Contract

Completion detection is opt-in because agent CLIs do not expose a structured "done" channel. Enable it by config or CLI:

```bash
codex-looper --complete-on 'EXIT_SIGNAL:\s*true'
```

Recommended prompt convention:

```text
End every reply with a status line. Emit EXIT_SIGNAL: true only when the work is genuinely complete. Otherwise emit EXIT_SIGNAL: false.
```

If `plan_file` is configured, a completion marker only counts when that markdown file has no unchecked `- [ ]` tasks. Markers seen while unchecked tasks remain reset the completion streak and the loop continues.

## Git Safety

`--backup` creates a branch before each non-dry-run loop:

```text
looper-backup/20260622T120000Z-loop-0001
```

Use `--backup-prefix` to name a separate backup family, and `--backup-keep` to prune older branches. `--backup-keep 0` disables pruning.

`--cb-no-progress N` stops after N completed loops where git `HEAD` and worktree status are unchanged. The looper ignores its own run log directory when computing this fingerprint.

`--cb-output-decline N` stops after N consecutive completed loops where captured stdout/stderr bytes decline versus the prior completed loop. This is a lightweight signal for loops that are producing less useful work over time. It is off by default.

Every completed loop prints a compact metrics line:

```text
loop metrics: loop=3 duration=1.25s output=1.5KiB
```

## Presets

Presets are ordinary TOML files layered over project config before CLI flags. A path works directly:

```bash
codex-looper --preset ./my-loop.toml
```

Named presets resolve from:

1. `~/.config/codexfarm/presets/<name>.toml`
2. `examples/presets/<name>.toml` in this repo

The repo includes `examples/presets/rai.toml`:

```bash
codex-looper --preset rai
```

That preset uses Claude, `single` mode, `PROMPT.md`, `EXIT_SIGNAL: true` completion, `fix_plan.md`, and git backups.

## Retry And Stop Conditions

The looper retries the current prompt when it sees provider rate-limit, backoff, quota, temporary-unavailability, or overload signals. If structured provider output includes a relative retry delay or reset timestamp, the looper waits for that delay; otherwise it falls back to the configured `sleep_seconds` delay. Informational rate-limit telemetry such as Claude `rate_limit_event` records with `status = "allowed"` or `status = "allowed_warning"` is ignored.

Rate-limit retries are uncapped so long-running loops can wait for quota reset and keep going. Non-rate-limit transient retries are capped by `max_transient_retries`; set it to `0` to allow unlimited transient retries. While waiting, the looper keeps the tmux window state as `RUN` and writes the retry attempt, retry kind, next wait duration, and reason into `@codex_stop_reason` for status tooling. Retry waits at or above `retry_notify_after_seconds` also emit a tmux display message; set it to `0` to disable long-wait notifications.

The looper stops when it sees:

- local per-prompt timeout
- common timeout, deadline, or request-abort wording
- nonzero command exit, unless `--ignore-nonzero` is set
- repeated non-rate-limit transient retry signals after `max_transient_retries`
- configured completion marker and satisfied plan gate
- configured no-progress circuit breaker
- configured output-decline circuit breaker
- configured max loop count

Logs are written under `.agent-looper/runs/<timestamp>__<label>/`.

## Farm Integration

Normal non-dry-run looper commands call `codex-add` so existing farm behavior still owns session creation, board linking, pipe-pane logging, and annotator startup. Use `--local` to run in the current terminal. Use `--farm-session NAME` for a separate farm.
Farm windows enable tmux `remain-on-exit` by default, so a stopped looper leaves its final pane visible for inspection instead of closing the window. Set `CODEX_REMAIN_ON_EXIT=0` when launching if you want the old close-on-exit behavior.
Looper labels are kept for logs and agent session names only. They do not rename tmux windows; farm window names come from `CODEX_NAME` when set, otherwise the working directory basename.

Example:

```bash
codex-looper
codex-looper --farm-session work --label cleanup --cwd /path/to/project
codex-looper --local --once --label local-smoke
```

While a looper is running in the farm, inspect recent pane output without attaching:

```bash
codex-status activity
```

## Current Limits

- It is a loop runner, not a scheduler, daemon, queue, or web UI.
- It does not bypass authentication, permissions, sandboxing, or provider limits unless you explicitly pass agent-native flags that do so.
- The Gemini backend is intentionally generic until a stable noninteractive resume interface is confirmed.
- Use write-enabled agent flags only in repositories, worktrees, containers, or runners where automated edits are acceptable.
