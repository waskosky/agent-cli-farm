# Agent Looper Reference

`codex-looper`, `claude-looper`, and `gemini-looper` run a file-backed sequence of prompts against local agent CLIs. Initialized loopers launch in the default Codex CLI Farm tmux session by default, so runs are inspectable without extra arguments.

Runtime requirement: Python 3.10 or newer. On Python 3.11+, the standard library TOML parser is used; on Python 3.10, the looper uses its built-in parser for the supported `agent-looper.toml` schema documented below.

## First Run

From the repository where the agent should work:

```bash
codex-looper
```

If `agent-looper.toml` and `prompts.md` are missing, this writes starter versions of both files and prints next steps. Existing files are left unchanged.

For guided setup:

```bash
codex-looper init --interactive --force
```

Interactive setup asks for:

- default agent
- per-prompt timeout
- sleep interval between loops
- maximum loop count
- one or more prompts

## Prompt File

The default prompt file is `prompts.md`. Separate prompts with a line containing only `---`:

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
claude-looper --once --label smoke
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
| `--config PATH` | `agent-looper.toml` | Config file path. |
| `--prompt-file PATH` | `prompts.md` | Prompt sequence file. |
| `--label LABEL` | `Looper_<short-id>` | Human-readable run/session label. |
| `--timeout SECONDS` | `7200` | Per-prompt subprocess timeout. |
| `--sleep SECONDS` | `2` | Sleep between completed loops. |
| `--max-loops N` | `0` | Maximum loops; `0` means unlimited. |
| `--once` | off | Equivalent to `--max-loops 1`. |
| `--fresh-session-per-loop` | on | Start a new agent session each loop. |
| `--reuse-session` | off | Reuse one session across all loops. |
| `--cwd PATH` | current directory | Working directory for agent commands. |
| `--dry-run` | off | Print commands without running agents. |
| `--ignore-nonzero` | off | Continue after nonzero agent exits. |
| `--stop-on-nonzero` | on | Stop after nonzero agent exits. |
| `--hold-on-stop` | off | Wait for Enter before closing after stop. |
| `--local` | off | Run in the current terminal instead of launching through the default farm. |
| `--farm-session [NAME]` | default farm | Select the farm tmux session for `codex-add`. Omitting `NAME` uses the default farm session. |
| `--farm-attach` | off | Attach after `--farm-session` launch. |
| `--farm-add-bin PATH` | `codex-add` | Launcher compatible with `codex-add`. |
| `-- AGENT_ARGS...` | unset | Pass native flags to built-in Codex/Claude command templates. |

Use `--` for one-off agent-native flags. For Claude, the correct spelling is
`--dangerously-skip-permissions`; local Claude help recommends it only for
isolated sandboxes. To make it permanent for a project, put the same values in
`[agents.claude].extra_args`.

## Config Defaults

Starter `agent-looper.toml` defaults:

```toml
[looper]
default_agent = "codex"
prompt_file = "prompts.md"
timeout_seconds = 7200
sleep_seconds = 2
fresh_session_per_loop = true
max_loops = 0
log_dir = ".agent-looper/runs"
```

Agent defaults:

| Agent | Command behavior |
| --- | --- |
| `codex` | First prompt: `codex exec --json <prompt>`; later prompts resume by Codex thread ID when available, otherwise `resume --last`. |
| `claude` | First prompt: `claude -p --output-format stream-json --verbose --name <session> <prompt>`; later prompts use `--resume <session>`. |
| `gemini` | Generic default: `gemini -p <prompt>` for every prompt. Override this if your Gemini CLI supports a better noninteractive/resume mode. |

Custom agents can use command templates:

```toml
[agents.my_agent]
kind = "generic"
first_command = ["my-agent", "run", "--session", "{session}", "{prompt}"]
resume_command = ["my-agent", "run", "--resume", "{session}", "{prompt}"]
scan_stdout_for_stop_patterns = true
```

Available placeholders: `{prompt}`, `{session}`, `{session_id}`, `{loop}`, `{prompt_index}`, `{label}`, `{run_dir}`.

## Retry And Stop Conditions

The looper retries the current prompt after the configured `sleep_seconds` delay when it sees provider rate-limit, backoff, quota, temporary-unavailability, or overload signals. Informational rate-limit telemetry such as Claude `rate_limit_event` records with `status = "allowed"` or `status = "allowed_warning"` is ignored.

The looper stops when it sees:

- local per-prompt timeout
- common timeout, deadline, or request-abort wording
- nonzero command exit, unless `--ignore-nonzero` is set

Logs are written under `.agent-looper/runs/<timestamp>__<label>/`.

## Farm Integration

Normal non-dry-run looper commands call `codex-add` so existing farm behavior still owns session creation, board linking, pipe-pane logging, and annotator startup. Use `--local` to run in the current terminal. Use `--farm-session NAME` for a separate farm.

Example:

```bash
codex-looper
codex-looper --farm-session work --label cleanup --cwd /path/to/project
codex-looper --local --once --label local-smoke
```

While a looper is running in the farm, inspect recent pane output without
attaching:

```bash
codex-status activity
```

## Current Limits

- It is a loop runner, not a scheduler, daemon, queue, or web UI.
- It does not bypass authentication, permissions, sandboxing, or provider limits unless you explicitly pass agent-native flags that do so.
- The Gemini backend is intentionally generic until a stable noninteractive resume interface is confirmed.
- Use write-enabled agent flags only in repositories, worktrees, containers, or runners where automated edits are acceptable.
