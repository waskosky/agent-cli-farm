# Agent CLI Farm - Agent Status Maintenance

## Source of truth

Status classification lives in code and tests, not in notes copied from another project.

- `bin/codex-annotator.py`
  - `classify_codex_output()`
  - `classify_claude_output()`
  - `classify_pane()`
  - `aggregate_window_state()`
- `tests/test_codex_annotator.py`
- `tests/test_annotator_status.py`

Update those tests whenever prompt, spinner, provider, or tmux-pane behavior changes.

## Classification model

The annotator classifies each pane from a combination of:

- Captured terminal output.
- `pane_current_command`.
- `pane_start_command`.
- Dead-pane state.
- Provider-specific prompt, processing, waiting, and error patterns.

Provider-specific output classification must run before generic running-command
heuristics. This matters for Node-installed CLIs where `pane_current_command` is
often `node`, while `pane_start_command` identifies Codex or Claude.

## UI meaning

- READY means the pane appears available or is waiting for user action.
- RUN means the pane shows an explicit running/processing signal.
- ERR means the pane shows an actionable error signal.

These states are heuristic because Codex, Claude, Gemini, and generic shells do
not expose a structured status channel through tmux.

## Operational rules

- Title rewriting is off by default; native CLI window-title updates should be
  allowed unless the user opts into managed status titles.
- Memory markers must be preserved when status prefixes are added or removed.
- Stale window state must be pruned during annotation passes.
- Invalid user-supplied patterns, templates, or intervals must fail cleanly and
  must not crash a long-running annotator daemon.

## Resource and background-service policy

- Normal setup and operation must not install privileged host guards, replace
  ordinary tools, or impose global CPU, RAM, swap, task, or time ceilings.
- Any explicitly requested resource enforcement must apply only to the requested
  job, with the proposed settings shown to the user before activation.
- Restore memory checks are advisory by default, with two-second launch spacing.
  `--enforce-memory-pressure` opts into refusal at critical pressure or a failed
  health helper; unknown counters remain advisory. `--ignore-memory-pressure`
  skips checks. `CODEXFARM_RESTORE_MEMORY_POLICY=warn|enforce|ignore` sets the
  default; CLI flags override it and invalid final policy fails before tmux changes.
- Autoservice defaults to lightweight manifests. Only explicit
  `--install-autoservice --with-conversation-backups` opts into scheduled full
  archives; `--without-conversation-backups` switches back. Persist archive
  consent separately in `conversation_backup_choice`; a legacy service yes is
  not archive consent. Explicit refreshes preserve the separate choice.
- Ordinary launches with stored autoservice yes register the farm without unit
  writes or manager activation. First-time interactive/environment yes installs
  once; explicit installs refresh units. Respect stored no and all operator masks.
  Precheck all three units for local/runtime/global masks before writing any
  units or choices; never unmask them.
- Disabled autoservice or archive choice suppresses stale archive warnings,
  except the explicit `CODEXFARM_BACKUP_HEALTH_ENABLED=1` override. Preserve
  backup files and retain opted-in archive budgets and priority safeguards.
- Annotator polling defaults to five seconds, preserving environment and CLI
  overrides and native provider titles.

## Managed job identity and consent

- `codex-job` and `codex_looper.resource_jobs` accept exact argv; never add shell
  evaluation or reinterpret provider arguments or trusted launcher fragments.
- Agent wrapping is opt-in through `CODEXFARM_RESOURCE_PROTECTION=1` or private
  `protect_agents` settings. Agent admission is always immediate; retain terminal
  behavior and the Looper-created process group. Agents never accept ceilings,
  restart requests, pause, or OOM-kill remedies.
- Batch queueing and all other resource features default off. Explicit batch
  memory limits require scope enforcement and fail before execution if unavailable.
  Normal optional scope failure can fall back only before the durable inner-start
  handshake; a payload error must never cause a second execution.
- Scope properties must be scope-supported. Apply batch nice/OOM preferences in
  the payload trampoline. Runtime slice preferences never write/unmask unit files
  and must preserve stronger or unknown MemoryLow values on both shared parent
  and interactive child. Apply role weights at sibling slices as well as scopes.
- All remedies require a live recorded UID, PID/start ticks, actual cgroup and
  scope generation. Never select by process name. Restart consent is batch-only,
  explicit, and limited to one request; termination targets only its dedicated
  launch group, with revalidated stable handles for surviving descendants.
- Worker overrides use only the numeric allowlist and exact recipe fingerprints;
  bounded TTL reductions and deferrals affect future launches. Existing jobs
  change worker counts only after a separately consented restart. Public metadata
  excludes argv, cwd and arbitrary environment values. Private state stays bounded
  with 0700 directories, 0600 atomic files, and stale-record cleanup. Never prune
  records with any live or unreadable recorded process identity; temporary scope
  query failures must leave running records recoverable.
- Keep tests in `tests/test_resource_config.py` and `tests/test_resource_jobs.py`,
  plus launcher and Looper regressions. Use private homes and systemd doubles for
  verification; do not install services or change live tmux sessions in tests.
