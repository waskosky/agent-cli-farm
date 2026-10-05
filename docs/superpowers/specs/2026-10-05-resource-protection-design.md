# Gentle memory protection and incident investigation

The user approved the recommendations in this conversation and explicitly
requested implementation, publication, and merging into `origin/main` on
2026-10-05. This document makes those already approved recommendations concrete.

## Outcomes and boundaries

- Running agents are never killed, suspended, or restarted because available
  memory crosses a threshold. Existing conversations are preserved during
  installation and rollout.
- Available-memory warning defaults are fixed amounts: 1536 MiB warning and
  1024 MiB critical. Linux memory stall thresholds remain an independent signal.
  Explicit legacy percentage settings continue to work.
- Optional background admission waits below 1024 MiB or sustained critical
  memory pressure. It releases after at least 1536 MiB and non-warning memory
  pressure for 30 seconds. Waiting has a timeout and an explicit bypass.
  Missing counters and hosts too small for the target remain advisory.
- Ordinary setup does not install privileged guards, replace ordinary tools,
  unmask services, or introduce global ceilings. Host protection has its own
  explicit, reviewable administrative command.
- This host's disabled full conversation backups and autoservice masks remain
  disabled. Exact recovery identifiers and small existing manifests remain
  available.

## Memory policy

`codex_looper/resource_policy.py` owns validated headroom settings and a pure
hysteresis gate. Its decisions never contain a kill or stop action.
`codex_looper/health.py` keeps its existing diagnostic and restore exit contracts,
shared-app-server accounting, monitor heartbeat, and advisory fallback behavior.

The fixed defaults do not grow with total RAM. A percentage policy remains
available for explicit compatibility overrides. Non-finite values, booleans
where real numbers are required, reversed thresholds, and invalid policies fail
before mutation or launch. Report memory and I/O stall counters separately;
previously used swap alone never activates an intervention.

## Managed jobs and agent launch integration

`codex-job` is an explicit argv-based runner with agent and batch roles. Normal
farm launches use its agent role only when resource protection was opted in.
Provider arguments retain the existing shell-fragment compatibility and exact
resume behavior. A failed optional scope setup warns and preserves access to an
agent; an explicitly requested hard limit fails before execution if enforcement
is unavailable.

Future agents and managed batch jobs use separate systemd user scopes and slices.
The `codexfarm.slice` parent and interactive slice have a shared 1 GiB `MemoryLow`
budget. Agent scope `MemoryLow=infinity` passes through that shared parent budget;
it does not create an independent reservation for each agent. The batch slice has
lower CPU and I/O weights, with no CPU quota, memory ceiling, swap ceiling,
or task ceiling by default. Batch processes get a modest positive OOM score;
interactive protection uses a modest negative score where privileges allow.
Optional per-job `MemoryHigh` and `MemoryMax` are shown in the command/help and
validated before launch. No generic `python`, `npm`, or other tool is wrapped.

Private job records contain generated IDs, process start identities, exact
scope identities, owner UID, role, state, and locally stored argv. Telemetry
never includes argv, environment secrets, conversation text, or executable
manifests. Records are written atomically with owner-only permissions.

An explicit `--restartable` declaration authorizes at most one graceful restart
of a batch job per run. Agents cannot be declared restartable. A registered
worker-count environment setting allows future worker-count reductions without
editing project files. Such overrides are scoped to an exact private command
recipe fingerprint and expire; applying a reduction to an already running job
requires that job's separately declared restart consent. Actions operate on exact live job identities, verify
ownership and process/scope generation, and never select processes by name or
largest RSS. A restart waits for recovered headroom before re-execution.

## Explicit host protection

`codex-resource-host` is a standalone administrative helper, usable without
importing user-writable modules after installation. Its plan displays the
protection settings, touched paths, and rollback operation before apply.
Applying installs root-owned code/config and snapshots existing managed paths.
It installs memory protection through the required user-slice hierarchy and
the interactive user slice. Existing stronger protection is preserved.

An optional small root-owned maintenance timer may assign the negative OOM score
only to the configured UID inside the dedicated interactive scopes, and positive
scores inside managed batch scopes. It must verify UID and actual cgroup
membership through `/proc`, bound its work, and never read or execute an LLM
response, user command, or arbitrary job recipe as root. Existing verified
agent PIDs can receive a one-shot score adjustment without moving or restarting
them; their existing session scopes receive the corresponding runtime protection
within the same shared parent budget. The helper imposes no workload limits, sends no signals to agents, and
does not alter the retired host guard or backup units.

Applying and removing are idempotent, refuse masked target units, preserve
unrelated drop-ins, propagate write/manager failures, and provide rollback
without stopping the farm. Persistence uses only dedicated owned paths and
explicitly requested timer activation. Removing restores settings for matching
process identities and owned paths without overwriting later operator edits.

## Incident investigation

`codex-resource` configures protection/admission/investigation independently and
offers current status, a bounded local report, manual investigation, and a
worker entry point. All optional features default off except fixed advisory
warnings. Configuration is private JSON with strict types and atomic writes.

The existing health monitor schedules a detached incident worker only after
60 seconds of sustained memory or I/O pressure. A private nonblocking lock
allows one investigator at a time, with a 15-minute cooldown, bounded retained
reports, a timeout, and a low-memory deferral. The monitor itself never waits for
an LLM. Monitoring failures remain advisory and do not stop annotation.

The default remote adapter uses the existing Codex CLI login. It executes with
private HOME, CODEX_HOME, and working directories. The private CLI home links
only the existing owner-only authentication cache; it does not copy credentials
into reports or print them. Normal native CLI authentication-cache refresh
remains allowed. It ignores user configuration/rules, uses ephemeral/read-only
mode and JSON-schema output, and disables available shell, browser, computer,
image, apps, plugins, hooks, skills, and multi-agent tooling. A worker-only model
catalog also clears model-derived shell, code-mode, collaboration, patch, and
usage-instruction metadata; accepted feature flags alone are insufficient.
A small fixed worker instruction file replaces generic coding instructions;
report input leaves room for native framing within the 16 KiB input budget.
Unsupported required capabilities/catalogs fail diagnostically, never fall back
to an unrestricted agent. The adapter has bounded input, output, time, and its
own low-priority execution; optional model/binary overrides are explicit.
Local fake-transport tests verify tool registration (including additional_tools
input items), rejected execution/patch/collaboration dispatch, and exclusion of
original user instruction/skill sentinels without invoking a remote model.

Reports include bounded process/cgroup growth samples, RSS/PSS attribution,
pressure, swap context, cgroup events, and sanitized managed-job metadata.
Process labels are untrusted input. Host capacity always uses `MemAvailable`.

The model returns diagnosis, evidence, proposed fixes, and structured actions.
The deterministic executor permits only actions on explicitly registered batch
jobs: reduce a declared worker count, defer optional work, or request the single
allowed graceful restart. Unknown actions, stale identities, agent targets,
arbitrary commands, and service/kernel changes are rejected. Automatic actions
are separately opt-in and journal before/after readings and results. Reversible
scheduling/worker changes have bounded duration and rollback on deterioration;
restarts are explicitly disclosed as non-reversible, bounded operations.
Other processes receive diagnosis and proposed fixes, with no automatic signals.

## Delivery and verification

Use a feature worktree based on upstream `cc15bc2`, retaining the newer farm
recovery and shared-server fixes. Follow test-first development for policy,
process identity, scope fallback, privileged plan/apply/rollback boundaries,
model isolation, and action authorization. Tests use private homes, proc trees,
systemd doubles, and tmux sockets; never create live host pressure.

Run repository-required lint, shell, full unit tests, validation/demo/recovery
integrations, and the pinned deep-history smoke test. Review spec compliance
and code quality before publication. Push a PR, wait for exact-head CI, merge
to main, synchronize the main checkout, and install helpers without provider
hook or autoservice changes. Activate authorized user-level options and verify
the host's existing masks/caps. If privileged apply requires the user's sudo
password, provide the exact prepared command with that remaining limitation.

## Primary documentation checked

- Linux memory, OOM, and CPU-weight semantics:
  https://docs.kernel.org/admin-guide/cgroup-v2.html
- Linux pressure monitoring: https://docs.kernel.org/accounting/psi.html
- Codex non-interactive mode:
  https://learn.chatgpt.com/docs/non-interactive-mode
- Codex configuration:
  https://learn.chatgpt.com/docs/config-file/config-reference
- Codex CLI options:
  https://learn.chatgpt.com/docs/developer-commands?surface=cli
