# Agent Looper Reference

`codex-looper`, `claude-looper`, and `gemini-looper` run repeated file-backed prompts against local agent CLIs. By default, they loop one prompt file forever with a fresh agent session each loop. Sequence mode, completion detection, task-plan gates, git backups, circuit breakers, and presets are available when a project needs them.

Runtime requirement: Python 3.10 or newer. On Python 3.11+, the standard library TOML parser is used. On Python 3.10, the looper uses its built-in parser for the supported `agent-looper.toml` schema documented below; do not rely on full TOML features outside simple tables, strings, booleans, finite numbers, and string arrays.

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

Prompt defaults are resolved exactly this way:

1. An explicit `--mode` wins.
2. Otherwise `[looper].mode` wins.
3. Otherwise a custom `--prompt-file` uses `single` mode.
4. Otherwise the legacy default file `prompts.md` implies `sequence`.
5. Otherwise `PROMPT.md` with `single` mode is used.

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

Prompts in one sequence share the same agent session. After a full sequence
completes, the default behavior is to start a fresh session for the next loop.
For hybrid Codex and Claude agents, this means terminating the prior TTY pane
and starting a new agent process while preserving the supervisor pane.

By default, the supervisor reloads `prompt_file` before each loop. Editing `PROMPT.md` or `prompts.md` while a looper is running affects the next loop without restarting the supervisor. Set `[looper].reload_prompt_each_loop = false` only when a run must keep the exact startup prompt text.

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

Use `--once` or `--max-loops N` for bounded runs. Without either, the looper repeats until a stop condition occurs. The `run` subcommand is optional: in an initialized directory, `codex-looper` and `codex-looper run` are equivalent. Non-dry-run commands launch through the default farm unless `--local` is set.

`--timeout` is a per-prompt wall-clock limit for the local agent process. The default is 7200 seconds. Short values such as `90` seconds can stop a long Claude/Codex tool call even when the provider is still working; raise it on the command line or in `[looper].timeout_seconds` for heavier prompts. A timeout records the run as `error` and exits with status 124.

## CLI Options

Values are resolved in this order: built-in defaults, project config, preset config, environment layout defaults where documented, and finally CLI flags. Agent-native argv after `--` is appended to built-in Codex/Claude command templates for that invocation. For Codex and Claude hybrid interfaces, those argv tokens are appended to the visible interactive pane command.

Numeric constraints are strict. Integer counters must be whole numbers and finite; values documented as nonnegative accept `0`; values documented as positive must be greater than `0`. Float seconds must be finite and nonnegative unless the row says positive.

| Option | Default | Constraint | Purpose |
| --- | --- | --- | --- |
| `--agent NAME` | `[looper].default_agent` or wrapper default | nonempty string | Selects an agent config from `agent-looper.toml`. |
| `--interface json\|hybrid` | agent config | enum | Override the selected agent interface for this run. `hybrid` is available for Codex and Claude. |
| `--config PATH` | `agent-looper.toml` | path string | Project config file path. Missing config is allowed. |
| `--preset NAME_OR_PATH` | unset | name or path string | Layer a preset TOML file over project config. Named presets resolve from `${XDG_CONFIG_HOME:-~/.config}/codexfarm/presets/` and repo `examples/presets/`. |
| `--mode single\|sequence` | `single`, with legacy inference | enum | Select prompt loading mode. |
| `--prompt-file PATH` | see prompt precedence below | path string | Prompt file. |
| `--label LABEL` | `Looper_<short-id>` | string | Human-readable run/session/log label. It does not rename tmux windows. |
| `--timeout SECONDS` | `7200` | positive finite float | Per-prompt subprocess timeout. |
| `--sleep SECONDS` | `2` | nonnegative finite float | Sleep between completed loops. |
| `--max-loops N` | `0` | nonnegative integer | Maximum loops; `0` means unlimited. |
| `--max-transient-retries N` | `12` | nonnegative integer | Cap non-rate-limit transient retries; `0` means unlimited. |
| `--retry-notify-after SECONDS` | `300` | nonnegative finite float | Show a tmux notification for retry waits at or above this threshold; `0` disables. |
| `--complete-on REGEX` | unset | valid regex string | Enable completion detection and stop after completed loops whose output matches the regex. |
| `--completion-streak N` | `1` | positive integer | Require N consecutive completed loops with the completion marker before stopping. |
| `--plan-file PATH` | unset | path string | Markdown checklist gate; completion requires no unchecked `- [ ]` tasks. |
| `--backup` | off | boolean flag | Create a git backup branch before each non-dry-run loop. |
| `--backup-prefix PREFIX` | `looper-backup` | nonempty string | Branch prefix for backup branches. |
| `--backup-keep N` | `10` | nonnegative integer | Prune older backup branches, keeping the newest N. `0` disables pruning. |
| `--cb-no-progress N` | `0` | nonnegative integer | Stop after N completed loops with no git workspace fingerprint change. |
| `--cb-output-decline N` | `0` | nonnegative integer | Stop after N consecutive completed loops whose captured output byte count is lower than the prior loop. |
| `--cb-output-match REGEX` | unset | valid regex string | Stop after completed loop output matches the regex for the configured repeat count. |
| `--cb-output-match-repeats N` | `1` | positive integer | Matching loops required before `--cb-output-match` stops the run. |
| `--once` | off | boolean flag | Equivalent to `--max-loops 1`. |
| `--fresh-session-per-loop` | on | boolean flag | Start a new agent session for each completed loop. |
| `--reuse-session` | off | boolean flag | Reuse one agent session across all loops. |
| `--cwd PATH` | current directory | process cwd path | Working directory for agent commands. |
| `--dry-run` | off | boolean flag | Print commands without running agents or creating backup branches. |
| `--ignore-nonzero` | off | boolean flag | Continue after nonzero agent exits. |
| `--stop-on-nonzero` | on | boolean flag | Stop after nonzero agent exits. |
| `--hold-on-stop` | off | boolean flag | Wait for Enter before closing after a stop. |
| `--tmux-layout auto\|single\|split` | `CODEX_LOOPER_LAYOUT` or `auto`; farm launch sets `split` | enum | Split creates a second tmux pane that tails the active prompt log while the main pane keeps supervisor status. |
| `--local` | off | boolean flag | Run in the current terminal instead of launching through the default farm. |
| `--farm-session [NAME]` | default farm | optional session string | Select the farm tmux session for `codex-add`. Omitting NAME uses the default farm session. |
| `--farm-attach` | off | boolean flag | Attach after `--farm-session` launch. |
| `--farm-add-bin PATH` | `codex-add` | executable name or path | Launcher compatible with `codex-add`. |
| `-- AGENT_ARGS...` | unset | argv tokens | Pass native flags to built-in Codex/Claude command templates. |

Use `--` for one-off agent-native flags. For Claude, the correct spelling is `--dangerously-skip-permissions`; local Claude help recommends it only for isolated sandboxes. To make it permanent for a project, put the same values in `[agents.claude].extra_args`.

`codex-looper` and `claude-looper` default to `interface = "hybrid"`. The hybrid interface starts a visible agent TTY pane, pastes prompts into that pane, reads the agent's session JSONL files for session identity and turn advancement, and keeps the supervisor pane focused on loop state. Use `--interface json` or `[agents.codex].interface = "json"` / `[agents.claude].interface = "json"` to force the older noninteractive JSON stream path.

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
reload_prompt_each_loop = true
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
cb_output_match_pattern = ""
cb_output_match_repeats = 1
```

Agent defaults:

| Agent | Command behavior |
| --- | --- |
| `codex` | Default hybrid: start a visible `codex --no-alt-screen` TTY pane, paste prompts, and detect turn completion from pane readiness plus Codex session JSONL advancement. With `fresh_session_per_loop = true`, replace the pane/process between loops; otherwise reuse it. With `interface = "json"`: first prompt uses `codex exec --json <prompt>`; later prompts resume by Codex thread ID when available, otherwise `resume --last`. |
| `claude` | Default hybrid: start a visible `claude` TTY pane, paste prompts, and detect turn completion from Claude session JSONL terminal events plus pane readiness. With `fresh_session_per_loop = true`, replace the pane/process between loops; otherwise reuse it. With `interface = "json"`: first prompt uses `claude -p --output-format stream-json --verbose --name <session> <prompt>`; later prompts use `--resume <session>`. |
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
interface = "hybrid"
model = "claude-opus-4-8"
effort = "max"
```

Use `interactive_command` when hybrid mode should launch a wrapper or profile command instead of the built-in interactive executable:

```toml
[agents.codex]
kind = "codex"
interface = "hybrid"
interactive_command = ["codex-wrapper", "--profile", "loop"]

[agents.claude]
kind = "claude"
interface = "hybrid"
interactive_command = ["claude-wrapper", "--profile", "loop"]
```

Custom `first_command`, `resume_command`, and `interactive_command` values fully control their argv, so include model or effort flags directly in those values when using custom commands.

Strict TOML typing is part of the contract:

| Config key family | Required type |
| --- | --- |
| `looper.default_agent`, `mode`, `prompt_file`, `log_dir`, `completion_marker`, `plan_file`, `backup_prefix`, `cb_output_match_pattern` | string |
| `looper.timeout_seconds`, `sleep_seconds`, `retry_notify_after_seconds` | finite integer or float |
| `looper.max_loops`, `max_transient_retries`, `completion_streak`, `backup_keep`, `cb_no_progress`, `cb_output_decline`, `cb_output_match_repeats` | integer |
| `looper.fresh_session_per_loop`, `reload_prompt_each_loop`, `completion_enabled`, `backup_enabled` | boolean |
| `agents.<name>.kind`, `interface`, `model`, `effort` | string |
| `agents.<name>.extra_args`, `interactive_command`, `first_command`, `resume_command`, `stop_patterns` | array of strings |
| `agents.<name>.scan_stdout_for_stop_patterns` | boolean |

Wrong scalar types, non-finite numbers, invalid regexes, and empty values for documented nonempty fields fail during configuration instead of being coerced silently.

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

Backup branches point to the current committed `HEAD`; they do not snapshot uncommitted or untracked worktree contents. Commit or stash important dirty work before relying on backup branches.

Pruning is scoped to the exact configured namespace. For example, a prefix of `looper-backup` may prune `looper-backup/...` branches but not `looper-backup-old/...`.

`--cb-no-progress N` stops after N completed loops where the git workspace fingerprint is unchanged. The fingerprint includes committed `HEAD`, porcelain status entries, tracked index metadata, and file contents for dirty and untracked paths. The looper ignores its own run log directory when computing this fingerprint.

`--cb-output-decline N` stops after N consecutive completed loops where captured stdout/stderr bytes decline versus the prior completed loop. This is a lightweight signal for loops that are producing less useful work over time. It is off by default.

Output-decline is byte-count based. It does not judge semantic quality, and it can be fooled by verbose low-value output or concise high-value output.

`--cb-output-match REGEX --cb-output-match-repeats N` stops after N consecutive completed loops whose captured logs match the configured regex. Use it for project-specific status lines such as repeated blocked reports while keeping the looper engine independent of any one prompt format. A non-matching loop resets the streak.

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

1. `${XDG_CONFIG_HOME:-~/.config}/codexfarm/presets/<name>.toml`
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
- configured output-match circuit breaker
- configured max loop count

Logs are written under `.agent-looper/runs/<timestamp>__<label>__<random>/`. Run directory names use UTC time with subsecond precision plus a random suffix to avoid collisions. The `.agent-looper/current-log` pointer is updated atomically so the split tmux control pane either sees the previous complete pointer or the next complete pointer.

Each run directory also contains durable machine-readable status:

- `state.json`: latest snapshot with schema version, pid, label, agent, cwd, current loop/prompt, current session, status, stop reason, exit code, and last log path.
- `events.jsonl`: append-only lifecycle history for run start, loop start/end, prompt start/end, retry waits, and final stop.
- `control.jsonl`: optional append-only operator inbox. Queue `stop_after_loop`, `stop_after_prompt`, or `interrupt_now` commands with `codex-looper control stop ...`; the supervisor records consumed commands in `events.jsonl` and stops at the requested safe boundary.
- `focus.jsonl`: append-only current-focus history. Use `codex-looper control focus ...` to record a concise, human-readable sentence about the larger stroke of work the agent is currently selecting.
- `operator_notes.jsonl`: append-only operator-note history. Use `codex-looper control note ...` to record a note, paste it into the active hybrid pane as Claude `/btw`, or paste it as a normal prompt when that is the explicit operator intent.

Use `codex-status loopers` from a project root to read `.agent-looper/runs/*/state.json` without attaching to tmux. Active states whose supervisor pid is gone or defunct are shown as `stale`. Use `codex-status loopers --repair-stale-loopers` to mark those stale states `stopped`, append a `run_stopped` event, and record an external-termination stop reason. Set `CODEX_LOOPER_STATE_ROOT=/path/to/runs` to point it at an aggregated or remote-synced run directory.

Examples:

```bash
codex-looper control stop LOOPER-rai --after-loop --reason "merge checkpoint"
codex-looper control stop LOOPER-rai --after-prompt --state-root /path/to/.agent-looper/runs
codex-looper control stop --run-dir .agent-looper/runs/20260628T000000Z__LOOPER-rai__abc123 --now
codex-looper control stop LOOPER-rai --force --grace-seconds 5
codex-looper control focus LOOPER-rai --summary "Checking that screenshot validation still has a working live path."
codex-looper control focus --run-dir .agent-looper/runs/20260628T000000Z__LOOPER-rai__abc123 --summary "Retiring stale orchestrator assumptions before another loop run."
codex-looper control note LOOPER-rai --delivery btw --note "camera drift was fixed after controller calibration"
codex-looper control note LOOPER-rai --pane %12 --delivery btw --note "send this to the explicit pane"
codex-looper control note --run-dir .agent-looper/runs/20260628T000000Z__LOOPER-rai__abc123 --delivery record --note-file ./handoff-note.md
```

`--now` queues `interrupt_now` and sends SIGINT to every known runtime target: the child process group, hybrid pane descendants and process group when present, and the supervisor process. `--force` implies `--now`; after the grace interval it escalates still-running targets through SIGTERM and then SIGKILL, then attempts stale-state repair for the run directory. Do not combine `--force` with `--after-loop` or `--after-prompt`; those are cooperative safe-boundary stops.

Farm-launched Claude hybrid layout uses the supervisor pane as the merged status/control surface and keeps the second pane dedicated to the live Claude Code TTY. The supervisor banner shows the selected run, status basics, latest focus sentence, run directory, and command-based controls such as `codex-looper control focus|stop|note --run-dir ...`. Focus updates written during a prompt are printed back into the supervisor pane as they arrive. Non-hybrid split layout still starts a looper control pane below the supervisor. That helper pane shows the selected run, current status, latest focus sentence, current log path, a rendered live transcript, and bounded key actions: `b NOTE` send an operator `/btw` note, `p` stop after prompt, `l` stop after loop, `i` interrupt now, `f` force stop, `r` repair stale state, and `q` quit the pane. Actions read `state.json`, append durable records, and call the shared note/stop-target functions so CLI and pane behavior stay identical. Prefer `--delivery record` or `b NOTE` for contextual operator updates; use `--delivery prompt` only when the operator intends to add an ordinary user prompt to the live pane. When `state.json` does not identify a hybrid pane, pass an explicit `--pane`; broad tmux pane scanning requires `--allow-pane-scan` after verifying the intended recipient.

Prompt authors should gently ask agents to refresh the focus when they select or materially change the overall job or role:

```bash
codex-looper control focus --run-dir "$CODEX_LOOPER_RUN_DIR" --summary "Reviewing orchestrator defaults before restarting the live validation loop." --actor agent --source prompt
```

Keep the summary to one high-level sentence. Do not use the focus log for test output, rationale chains, substep narration, or ordinary operator instructions; those belong in logs, commits, plans, or operator notes.

## Farm Integration

Normal non-dry-run looper commands call `codex-add` so existing farm behavior still owns session creation, board linking, pipe-pane logging, and annotator startup. Use `--local` to run in the current terminal. Use `--farm-session NAME` for a separate farm.
Farm windows enable tmux `remain-on-exit` by default, so a stopped looper leaves its final pane visible for inspection instead of closing the window. Set `CODEX_REMAIN_ON_EXIT=0` when launching if you want the old close-on-exit behavior.
Looper labels are kept for logs and agent session names only. They do not rename tmux windows; farm window names come from `CODEX_NAME` when set, otherwise the working directory basename.
Farm-launched loopers default to `CODEX_LOOPER_LAYOUT=split`: the main pane shows the looper supervisor. Claude hybrid runs use the second pane for Claude Code itself, so focus/status/control information is surfaced in the supervisor pane. Non-hybrid split runs use the second detached pane as the looper control pane and live transcript for the current `.agent-looper/runs/.../loop-*.log` file. Use `--tmux-layout single` or `CODEX_LOOPER_LAYOUT=single` to keep the older one-pane view. If tmux split-pane creation fails, the looper keeps running and falls back to supervisor-pane streaming so the transcript is still visible.

Example:

```bash
codex-looper
codex-looper --farm-session work --label cleanup --cwd /path/to/project
codex-looper --local --once --label local-smoke
```

While a looper is running in the farm, inspect recent pane output without attaching:

```bash
codex-status activity
codex-status loopers
```

## Current Limits

- It is a loop runner, not a scheduler, daemon, queue, or web UI.
- It does not bypass authentication, permissions, sandboxing, or provider limits unless you explicitly pass agent-native flags that do so.
- Provider status and stop detection are heuristic because Codex/Claude/Gemini CLIs do not expose a shared structured status protocol.
- Claude hybrid mode requires tmux because the real Claude interface lives in a managed pane. Use `--interface json` for local non-tmux runs or scripted noninteractive behavior.
- The Gemini backend is intentionally generic until a stable noninteractive resume interface is confirmed.
- There is no per-worktree run lock. Starting multiple write-enabled loopers against the same checkout can race or overwrite work.
- Backup branches protect committed `HEAD`, not dirty worktree state.
- Use write-enabled agent flags only in repositories, worktrees, containers, or runners where automated edits are acceptable.
