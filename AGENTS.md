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

- Normal setup and operation must not implicitly install privileged host guards,
  replace ordinary tools, or impose global CPU, RAM, swap, task, or time ceilings.
  Setup may offer the explicitly and separately consented host memory and
  Windows/WSL thermal steps below.
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

## Explicit host memory protection

- `bin/codex-resource-host` is standalone Python 3.10+ stdlib administrative code.
  It must never import the user-writable farm package, execute recipes/providers,
  select processes by name, move sessions, or restart workloads.
- Setup copies the helper and offers an informed default-no memory choice only
  with both stdin/stdout TTYs. Enable only `protect_agents` and `queue_background`
  via validated settings, preserving numeric thresholds and independent AI options.
  Enter/no/EOF and `--without-memory-protection` preserve every existing setting.
  Default unattended setup must not read/write settings, prompt or call host tools.
  `--with-memory-protection` explicitly enables user settings; unattended runs
  print manual commands only. Reject conflicting flags before installation writes.
- `plan --uid UID` is read-only; only explicit root
  `apply --uid UID` installs the root-owned helper/config and dedicated drop-ins.
  `--with-maintenance` explicitly adds its own 30-second root timer.
- After user enable, supported interactive Linux/non-root setup may run the
  installed neighbor helper's complete plan using fixed `/usr/bin/python3 -I`,
  actual positive UID and `--with-maintenance`. Explain ancestor preferences,
  other users' named farm-unit drop-ins and reclaim costs, then ask a separate
  default-no question before the exact sudo apply. Never invoke sudo on a failed
  preview, unsupported host, root account, unattended run, no or EOF. Accepted
  apply failures return nonzero with saved user settings; never remove differing
  config, reinstall or unmask to recover. Root paths/interpreter stay fixed.
- Preserve stronger/unknown MemoryLow values. The 1 GiB shared hierarchy protects
  used memory, never reserves free RAM, and gives no OOM immunity. Interactive
  CPU/IO weights are 200 versus batch 25; never introduce ceilings here.
- Precheck every target mask, including user local/runtime/global locations, before
  mutation. Read user-home paths only through the isolated UID-dropped mask probe.
  Never unmask host guards, build slices, autosave, or helper targets.
- Root OOM changes require configured UID, start ticks, actual exact dedicated
  scope (or explicitly supplied existing session), and stable proc directory FDs.
  Maintenance scans at most 4096 PIDs for two seconds, with a bounded private
  restoration journal. Preserve later operator changes and reused PID identities.
- Use `tests/test_resource_host.py` with private roots/proc trees and manager
  doubles. Never install host protection or mutate actual services in tests.

## Opt-in thermal safeguards

- Thermal telemetry and enforcement default off in separate private settings.
  Windows hardware polling uses only the dedicated, administrator-owned sensor
  helper; annotation reads bounded local data and never awaits Windows or an LLM.
- Interactive WSL setup may offer sensors and managed batch protection, then a
  separate default-no whole-machine cooling choice. Explicit
  `--with-windows-cooling` or yes after its 80%/boost-off explanation grants the
  exception to job-only enforcement for that power profile only. Never imply
  cooling consent from sensors, memory choices or a previous install. Default
  unattended setup and declined/skipped choices must not probe Windows or change
  settings. Explicit unattended flags print manual steps only; never elevate.
- When thermal enforcement is enabled, run CPU-heavy builds, full test suites,
  browser automation and rendering through `codex-job run --role batch --scope
  required -- <exact argv>`. Keep interactive providers in the agent role.
- Missing/stale temperatures queue new managed batch launches. Only registered
  batch jobs with revalidated UID, start ticks, cgroup and scope generation may
  receive temporary CPU quotas. Preserve stronger/unknown quotas and later
  operator changes; persist rollback before applying a remedy. Never cap or pause
  agents or introduce host-wide limits through the batch guard. Only the separately
  consented Windows profile may affect whole-machine processor performance.
- Tests use synthetic sensor files, private homes and manager doubles. Never
  install drivers, tasks or services or mutate live tmux sessions from tests.

## Reliability and recovery invariants

- Sensor recovery may control only its exact administrator-owned task instance,
  after checking principal/actions/enabled state. Serialize it with installation;
  retain bounded retry cooldowns and never reinstall a driver as recovery.
- Thermal admission requires a compatible live guard heartbeat and fresh sensor,
  not merely configured enforcement. Keep the 64-job remedy capacity reserved
  atomically before launch. Manager validation and remedies share a two-second
  pass budget; incomplete coverage must remain visible.
- Explicit CPU/concurrency/memory reservations remain batch-only and default off.
  Reservations retain orphaned/live/unreadable owners; memory overrides never
  bypass explicit reservations or thermal protection. Never reinterpret argv.
- Optional aggregate history excludes commands/paths/environment/conversations,
  has both age and byte caps, and cannot terminate enforcement on history failure.
- Exact capture receipts, bounded restore outcomes and retry units never imply
  conversation replay. Keep active writers unique and detached successes alive.
  History retention is separately selected and preserves operator-modified data.
- Archives are verified before latest-pointer publication/pruning. External copies
  and workspace snapshots require explicit destinations/selections. Extraction
  accepts only a new private tree and rejects unsafe links, paths and budgets;
  it must never replace a live provider database. Preserve independent archive no.
- Thermal-enrolled archives save lightweight manifests before waiting for managed
  batch admission; heavy work cannot postpone that invocation's capture.
- Verification uses private homes/trees and doubles, plus native Windows file I/O
  self-tests without hardware/task changes. Do not simulate a host crash or reboot
  the working farm to prove recovery.

## Optional incident investigation

- `codex-resource` configuration only writes private settings. All four independent
  options default off; no daemon/timer, host guard or backup changes are implicit.
- Health status is written before optional scheduling. Sustained pressure needs
  60 seconds, one global nonblocking lock, 15-minute cooldown and at least 512 MiB
  MemAvailable. Unknown samples/failures stay advisory; annotation never awaits LLMs.
- Reports must bound PID scans/time, sanitize labels, omit recipes/argv/cwd/env and
  conversations, and retain private 64 KiB files with 20-report/journal limits.
  Keep full trusted identity snapshots local; model projection chooses job IDs only.
- Codex isolation requires private HOME/CODEX_HOME/cwd, auth-only link, no user
  config/rules, read-only ephemeral execution, normalized bundled model metadata,
  disabled tooling and fixed instructions. Bound probes and provider time/output.
  Never ship fake provider routing, parse credentials or fall back unrestricted.
- Validate the entire strict output before mutations. Manual actions need explicit
  CLI intent plus stored consent; scheduled actions need stored consent. Recheck
  consent after inference and job identity/UID/cgroup/generation before each remedy.
  Only registered batch deferral, declared worker reduction and one consented
  restart request are allowed. Restarts are nonreversible requests. TTL rollback
  removes only this investigation's own reversible overrides.
- Test with synthetic authentication and loopback SSE; no live model or login calls.
  `CODEXFARM_TEST_NATIVE_CODEX=/absolute/native/codex python3 -m unittest
  tests.test_resource_investigator` runs the optional native isolation matrix,
  checking actual additional_tools, forbidden dispatch and final JSON delivery.
