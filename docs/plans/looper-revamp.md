# Looper revamp — single-prompt-endless default, fully-featured opt-in

Status: **design / proposal** (2026-06-22). No code yet — review first.

## Why

Today there are two loopers in play:

- **`codex-looper`** (this repo): multi-tool (codex/claude/gemini), file-backed
  **`---`-split prompt *sequence***, with strong reset-aware rate-limit retry —
  but no completion detection, task queue, git backups, or circuit breaker.
- **rai-loop** (in `waskosky/rai`, `scripts/rai-loop/`): a single **claude-only**
  autonomous loop that wraps the third-party **ralph-claude-code**. It adds
  completion/`EXIT_SIGNAL` detection, a task-queue gate, git backups, and a
  circuit breaker — but has no orchestration layer and isn't a tool we own.

The decision: **consolidate into the engine we own** (`codex-looper`) rather than
keep depending on vendored ralph for the "smart" half. Grow `codex-looper` so its
**default** is the simple, good model — *one prompt file, looped endlessly,
fresh context each loop* — and make everything else opt-in. This supersedes the
"keep two engines / don't merge" recommendation in the rai repo's
`scripts/rai-loop/codex-cli-farm-comparison.md`.

Ownership is the crux: ralph is third-party (extending it = forking); this repo is
ours, in Python we already maintain, and it already has the farm + multi-tool +
rate-limit retry that ralph lacks.

## Goal

`codex-looper`, revamped:

- **Default (zero config): single prompt file, loop endlessly, fresh session each
  loop.** Dead simple — the Ralph technique.
- **Fully featured on demand:** completion detection, task-queue gate, git
  backups, circuit breaker, model/effort — all **off by default**, on via config
  or flags.
- **Keeps today's strengths:** multi-tool agents, farm integration
  (`--farm-session`, annotator status, snapshot/restore, systemd persistence),
  reset-aware rate-limit retry.
- **rai-loop becomes a thin preset** over this engine; the ralph dependency for
  the rai project retires.

## Current state (grounding)

`bin/codex-looper.py` config (`[looper]` in `agent-looper.toml`) today:
`default_agent` (`codex`), `prompt_file` (`prompts.md`), `fresh_session_per_loop`
(`true`), `max_loops`, `sleep_seconds`, `timeout_seconds`, `max_transient_retries`,
`retry_notify_after_seconds`, `stop_patterns`, `kill_on_stop_pattern`,
`ignore_nonzero`, `scan_stdout_for_stop_patterns`, `log_dir`. Agents are `codex` /
`claude` / `generic` with `first_command` / `resume_command` templates. Loops run
a sequence of `---`-split prompts; it stops on max-loops, stop-pattern, nonzero
exit, transient-retry cap, or timeout. **No semantic "done" detection.**

## Design

### Modes

Add `[looper].mode`:

- **`single` (NEW default):** ignore `---` splitting; the whole `prompt_file` is
  one prompt, sent once per loop, **looped forever** (subject to the stop
  conditions below). Default `prompt_file` for this mode: `PROMPT.md`.
- **`sequence` (current behavior):** the existing `---`-split list; default
  `prompt_file` stays `prompts.md`.

### Default behavior (no `agent-looper.toml`)

`codex-looper` with just a `PROMPT.md` present → `mode=single`, default agent,
`fresh_session_per_loop=true`, `max_loops=0` (endless). Nothing else on. That is
the headline experience: *one prompt, one file, endless.*

### Opt-in features (all default OFF)

| Feature | New config (`[looper]`) / flag | Behavior |
|---|---|---|
| Completion / exit | `completion_enabled=false`, `completion_marker="EXIT_SIGNAL:\\s*true"`, `completion_streak=1` / `--complete-on REGEX` | Stop the loop when the agent's output matches `completion_marker` on `completion_streak` consecutive loops. The portable version of ralph's `EXIT_SIGNAL`. |
| Task-queue gate | `plan_file=""` / `--plan-file PATH` | A markdown checklist; the loop is "done" only when it has **no unchecked `- [ ]`** *and* a completion signal fired. Mirrors ralph's `fix_plan.md`. |
| Git backups | `backup_enabled=false`, `backup_prefix="looper-backup"`, `backup_keep=10` / `--backup` | Per-loop backup branch before each iteration; auto-prune to newest `backup_keep`. |
| Circuit breaker | `cb_no_progress=0`, `cb_output_decline=0` / `--cb-no-progress N` | Stop after N loops with no git change / collapsing output. (`cb_same_error` already partly covered by `max_transient_retries`.) |
| Model / effort | already pass-through via agent `extra_args` / `-- AGENT_ARGS` | e.g. `--model claude-opus-4-8 --effort max`. Optionally add `[agents.*].model`/`effort` sugar. |

### Keep as-is

Multi-tool agents; `--farm-session`/`--local`; rate-limit retry (reset-aware,
uncapped RL / capped transient, long-wait notify); per-pane logging via the farm;
`stop_patterns`.

### Completion contract

Agents have no structured status channel, so completion is **a marker in the
agent's own output**, instructed by the prompt. Default marker matches
`EXIT_SIGNAL: true`. Recommended prompt convention (portable, ralph-compatible):

```
End every reply with a status line. Emit `EXIT_SIGNAL: true` ONLY when the
work is genuinely complete (and, if a plan file is configured, every `- [ ]`
is checked). Otherwise emit `EXIT_SIGNAL: false` and keep going.
```

`completion_marker` is a regex so each user/agent can pick their own sentinel; the
optional structured `---STATUS---` block ralph uses is just one such convention.

## Backwards compatibility & rai-loop migration

- **Existing `prompts.md` users:** set `mode=sequence` (or auto-infer: if
  `prompts.md` exists and no `PROMPT.md`/explicit `mode`, default to `sequence`).
  Decide the inference rule (see Open Questions).
- **rai-loop → preset:** the rai project's loop becomes a `codex-looper`
  invocation: `agent=claude` (+ `--dangerously-skip-permissions` and
  `--model claude-opus-4-8 --effort max` via `extra_args`), `mode=single`,
  `prompt_file=<backlog-clearing prompt>`, `completion_enabled=true`,
  `plan_file=<fix_plan>`, `backup_enabled=true`, optionally run via
  `--farm-session`. `~/bin/rai-loop` shrinks to a wrapper that calls this preset;
  the ralph install + `.ralphrc` retire. (Keep ralph as a fallback until the
  preset is proven.)
- Provide a named-preset mechanism (e.g. `codex-looper --preset rai`, presets in
  `~/.config/codexfarm/presets/` or a repo `examples/`).

## Phased implementation

1. **MVP — single mode + completion.** Add `mode=single` (+ make it the default),
   and `completion_enabled`/`completion_marker`/`completion_streak`. This alone
   lets `codex-looper` do the rai-loop core (single prompt, endless,
   stop-when-done) for any agent. Tests for both modes.
2. **Safety — backups + circuit breaker.** `backup_enabled`(+prune) and
   `cb_no_progress`.
3. **Queue + presets + migration.** `plan_file` gate; preset mechanism; migrate
   rai-loop onto `codex-looper` and retire the ralph wrapper; update the rai repo
   docs/config.
4. **Polish (optional).** Per-loop metrics, richer status, model/effort sugar.

## Open questions

- **Default-mode inference:** is `mode=single` the unconditional default, or do we
  infer `sequence` when only `prompts.md` exists (to avoid surprising existing
  users)? Proposed: default `single`; infer `sequence` if `prompts.md` exists and
  neither `PROMPT.md` nor an explicit `mode` is set.
- **Completion strictness:** marker-only vs. require a structured status block.
  Proposed: marker regex by default; structured block optional.
- **Where rai's preset/config lives:** a `codex-looper` preset (this repo) vs. an
  `agent-looper.toml` committed in the rai repo. Proposed: preset here, thin
  `~/bin/rai-loop` wrapper.
- **Keep ralph as fallback** during/after migration? Proposed: yes, until the
  preset has run a few real backlog-clearing sessions cleanly.

## Cross-references

- rai-loop today: `waskosky/rai` → `scripts/rai-loop/README.md` (+
  `codex-cli-farm-comparison.md`).
- This repo's looper doc: [`../looper.md`](../looper.md).
