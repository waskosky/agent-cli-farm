# Gentle resource defaults: incident findings and approved design

Status: approved by the user on October 2, 2026, including implementation,
verification, publication, and merging to origin/main.

## What caused the incident

Two separate mechanisms affected the shared VM.

### Privileged host guard

The host had a custom `/usr/local/libexec/raintech-host-guard.py`, configuration
under `/etc/raintech-host-guard`, a system timer running every 30 seconds,
replacement tool symlinks in both `~/bin` and `~/.local/bin`, a user build slice,
and a resource-control drop-in on the farm restore service. None of these host
guard components or their installer occurs in this repository's current tree
or available Git history.

The replaced pytest entry point dates to October 1, 2026 at 21:27 UTC. The
configuration was replaced at 22:12 UTC and the guard program at 22:37 UTC.
These timestamps establish an installation window and later edits, rather
than identifying the person or agent that performed them. The readable shell,
agent, and application records do not contain the original installation command.
Administrator authentication logs are needed to complete that attribution.

The settings were fixed shared ceilings on a VM with two CPUs and about 7.7 GiB
RAM:

| Workload group | CPU quota | Memory high / maximum | Swap maximum | Task maximum |
| --- | --- | --- | --- | --- |
| Existing tmux development session, including its agent descendants | One CPU | 1.75 / 2 GiB | 1 GiB | 512 |
| Shared build/test slice | 0.75 CPU | 0.75 / 1 GiB | 0.25 GiB | 256 |

The session limits applied to the group containing the tmux server, rather than
to each individual chat. The monitor reapplied them even when its own host
reading was healthy. The build wrappers admitted only one heavy job at a time,
rejected additional work with exit 75, required 2 GiB available host RAM, and
imposed a 40-minute job timeout. These controls were independent of actual
headroom and of a job's real requirements.

The affected session's cumulative kernel counters, read after disabling the
guard, included 2,902,868 memory-high events, 46,667 CPU throttling periods,
about 1,827 seconds of accumulated CPU throttling, and 299,961 swap-limit
failures. Its OOM-kill counter was zero. These are cumulative counters, not a
precise before/after measurement or proof that every event came from this guard.
They support excessive throttling and reclamation as an explanation for poor
responsiveness. The final guard status recorded roughly 4.4 GiB RAM available
while still marking the session limited.

The guard timer and service are now masked and inactive. The wrappers and
persistent farm caps are removed, and existing session/build caps are cleared.
The build slice can remain active with unlimited settings until its existing
users release it; disabling must not stop working agents.

### Farm autosave expansion

Commit `60014d3fa549784dd8b7772908c03eb59d457d2f`, introduced on September 18,
2026 and merged in PR #18, changed the autosave service from small manifest saves
to `codex-backup --min-age 3600`. That command also stages coherent SQLite
copies and compresses provider histories. The five-minute timer retries this
work, with full snapshots intended hourly. A previous local incident report
recorded substantial disk traffic and unfinished archives after timed-out runs.

`bin/codex-add` additionally reinstalls and starts services whenever the saved
autoservice choice is yes. That couples opening a window to service activation
and can revive a service the operator disabled. `codex-restore` currently stops
on critical memory readings by default. Neither feature creates the privileged
host guard's CPU/RAM quotas, but both make the farm more intrusive than needed.

The original health command also reproduced a false warning: it reported stale
or failed backups and a missing archive even though `autoservice_choice` is
`no` and both autosave units are masked. The health code uses the presence of
the farm registry to infer that archives should be scheduled, and does not
honor the deliberate opt-out.

During implementation preparation, origin/main advanced to `8a2c570`, which
already makes direct backups manifest-only unless `--archive` is supplied,
limits explicit archives to 512 MiB by default with a 1 GiB disk reserve, uses
lower scheduling priority, and removes registry-only archive health warnings.
This change builds on that fix and preserves those archive safeguards.

## Goal

Keep useful diagnostics and exact session recovery while making normal farm
use advisory, inexpensive, and respectful of operator choices. Ordinary agent
and build commands keep their normal resource allocation. Enforcing a limit or
scheduling full-history copies requires an explicit, separate choice.

## Options considered

1. **Recommended: advisory defaults with explicit opt-ins.** Keep memory
   diagnostics and spaced launches, restore lightweight manifest autosaves,
   separate archive consent, and stop window launches from activating services.
   This addresses both reported sources of disruption without hiding pressure.
2. **Change only restore behavior.** This is smaller, but leaves hourly archive
   load and the service-reactivation bug in the normal autoservice workflow.
3. **Keep blocking restore and reduce background load only.** This retains an
   enforced stop during recovery and needs the operator to override it, even
   when completing a restore is the preferred response.

## Proposed behavior

### Memory diagnostics and restore

- Keep the existing host-memory measurements, available-RAM percentages,
  pressure readings, RSS attribution, and warning notifications.
- Make `codex-health --restore-check` advisory by default: it reports pressure
  and returns success. Regular diagnostic `codex-health` exit codes still
  describe unhealthy readings.
- Make restores warn and continue by default, retaining the existing two-second
  spacing. A missing or broken advisory helper must not prevent recovery.
- Add `--enforce-memory-pressure` and
  `CODEXFARM_RESTORE_MEMORY_POLICY=warn|enforce|ignore`. Enforcement remains
  opt-in and preserves exit 3 before deleting or launching windows when critical
  pressure is detected. The existing `--ignore-memory-pressure` is retained.
- Command-line policy overrides the environment and propagates through
  `--all-registered` and Claude/Gemini wrappers. Invalid policies fail before
  any tmux mutation. Enforced checks must succeed before a forced replacement.
- These settings do not stop existing chats or add CPU, RAM, swap, process,
  timeout, or whole-account limits.

### Autoservices and archive consent

- Autoservice remains optional. Its default save command is
  `codex-save --autosave --all-registered`; it saves small recovery manifests.
- Add separate `--with-conversation-backups` and
  `--without-conversation-backups` installation options. Full-history archives
  require an explicit yes, stored separately from `autoservice_choice`.
- Existing historical autoservice consent alone does not count as consent to
  the newly separated archive feature. An explicit reinstall defaults legacy
  units to manifest-only. Preserve a separately recorded archive choice on
  subsequent explicit service refreshes unless the caller changes it.
- Keep direct manual `codex-backup --archive` available, with the existing
  archive budget and safeguards. The opted-in service invokes
  `codex-backup --archive --min-age 3600`. Do not run it against this host's
  real history, activate archives here, or delete the user's remaining history.
- Give background save/backup helpers lower CPU and I/O scheduling priority,
  without applying quotas to agents, sessions, tools, or the account.
- A normal window launch with a saved yes registers its farm only. It does not
  rewrite units, reload systemd, enable timers, or start restore services.
  The explicit installation command remains the way to refresh service files.
- Respect masked service definitions before writing files, including explicit
  install attempts. Report the mask instead of writing through `/dev/null` or
  attempting to remove the mask.
- Report archive health only when archive scheduling is expected; a farm
  registry alone is not evidence that full-history backups are enabled.
  Explicitly disabled autoservice/archive choices suppress stale scheduler
  warnings while leaving files intact. The explicit diagnostic override
  `CODEXFARM_BACKUP_HEALTH_ENABLED=1` continues to request archive checks.

### Background polling and repository guidance

- Increase the annotator's default interval from one second to five seconds.
  Retain `CODEX_ANNOTATOR_INTERVAL` and `--interval` for users wanting faster
  notifications. Native provider titles remain unchanged.
- Document that setup and normal farm operation must not install privileged
  host guards, replace ordinary build tools, or impose global resource caps.
  Any requested resource enforcement must be explicit and scoped to the
  intended job, with the user shown the proposed limits.
- Update command help, README, and agent guidance with the same defaults and
  migration behavior.

## Implementation boundaries

The change belongs in the shared implementations: `bin/codex-add`,
`bin/codex-restore`, `bin/codex-annotator.py`, and `codex_looper/health.py`, with
behavioral regressions in their existing test modules. Provider wrappers retain
delegation to the shared core. Existing snapshot preservation, exact provider
identity checks, and provider safety stops remain separate from resource
policy.

No system units, production farms, provider processes, archive watchers, or
conversation databases need to be started, stopped, or rewritten to test the
repo changes. The host's existing masks and disabled backup policy stay in
place. Updating installed helper copies follows verification and does not
restart active provider processes.

## Acceptance and verification

- Synthetic critical memory permits a default restore, emits a warning, and
  preserves pacing. Explicit enforcement stops before mutations and between
  launches. Ignore mode and registered-farm propagation work for all providers.
- Missing advisory checks do not block default recovery; invalid policy values
  fail cleanly before mutation.
- Default generated autosave units invoke manifest saving only; separately
  opted-in units invoke history backup. A later explicit choice change updates
  the backend without altering unrelated settings.
- Opening a window with stored autoservice consent never starts or re-enables
  services. Existing masks remain intact after failed installation attempts.
- Disabled archives do not trigger false scheduler warnings from old status
  files. Enabled archives still report genuine faults.
- Annotator defaults to five seconds and accepts explicit interval overrides.
- Run focused regressions, the full unittest suite, pinned Ruff, Bash syntax,
  ShellCheck, and the repository's isolated validation/demo/resume checks.
  Optional deep-history verification uses a private temporary installation of
  the checksum-pinned backend when it is not already installed.
- Independently review the final diff against this design before publishing.

## Remaining provenance question

The exact installer account, command, and originating conversation are not yet
established. Read-only administrator audit output for the October 1 installation
window can fill that gap. Do not attribute the host guard to the farm installer
or a particular agent without that evidence.
