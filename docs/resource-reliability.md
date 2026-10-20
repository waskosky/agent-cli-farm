# Resource safeguards and recovery

This guide describes the opt-in safeguards tracked in [issue #28](https://github.com/waskosky/agent-cli-farm/issues/28). They protect enrolled batch work and keep recovery data inspectable. They do not promise immunity from a hardware shutdown.

## Know which layer is working

`codex-thermal status` reports configuration, effective protection, verified scope coverage, and the number of rollback records. `codex-health` and `codex-doctor --check` return nonzero for a selected safeguard that is unavailable, stale, incompatible, or incomplete. The annotator reads bounded local receipts; it never waits for Windows or a model.

| Layer | Evidence | Default failure detection |
| --- | --- | --- |
| CPU sensor | Real bounded LibreHardwareMonitor sample, source timestamp | Sample older than 15 seconds is unavailable |
| Linux guard | Live recorded PID/start ticks/boot ID, source generation, settings fingerprint, wall and monotonic heartbeat | Guard older than 30 seconds is unavailable |
| Batch enforcement | Current UID, payload identity, exact cgroup and scope generation; manager result | Partial coverage or rejected quota updates are degraded |
| Sensor recovery | Administrator-owned watchdog receipt | One-minute task; receipt older than 180 seconds is unavailable |
| Approved Windows cooling | Cached active plan and verified AC/DC processor settings | Receipt older than 120 seconds is unavailable |
| Manifest capture | Complete pane coverage, receipt timestamp and matching manifest SHA-256 | Capture older than 15 minutes warns |
| Conversation archives | Separately selected archive schedule, result and inventory | Existing archive health thresholds apply only after consent |

A fresh temperature does not prove that the Linux guard is alive. An enabled service does not prove that its last remedy worked. A stale sample never becomes fresh by receiving a new timestamp.

The configured thermal example warns at **80 C**, holds new batches at **85 C**, and shares **200% CPU time** across enrolled batches at **90 C**. `100%` is one CPU's time. Recovery requires readings below **75 C for 60 continuous seconds**, measured with a monotonic clock. These are configurable values, not universal hardware trip points.

The separate Windows profile sets maximum processor performance to **80%** and disables boost on AC and battery. This affects Windows and WSL CPU work; it is not an 80% utilization cap. Original power-plan settings and undo instructions are preserved. A later operator choice produces a warning instead of silently being replaced.

Linux memory preferences protect up to 1 GiB of **used** memory through `MemoryLow`, with interactive versus batch CPU/I/O weights of 200 versus 25. They do not reserve free RAM, impose a RAM ceiling, or provide OOM immunity. The privileged memory helper remains independent, isolated administrative code.

## Install and upgrade

From an interactive WSL2 terminal:

```bash
./setup.sh --with-thermal-protection --with-windows-cooling
```

The two choices are independent. Omitting the cooling flag offers its separate default-no choice. Declining a choice preserves existing settings. Ordinary unattended setup does not read thermal settings, probe Windows, elevate, or enable safeguards. Explicit unattended flags print commands to run interactively.

Windows setup verifies the pinned download hashes and PawnIO installer signature. Existing disabled tasks/drivers and unexpected task actions are preserved. The administrator-owned sensor task polls every five seconds. Its dedicated watchdog runs each minute and can recover only the held instance of that exact sensor task. It checks task definition, SYSTEM principal, enabled state, driver state, and recent receipts before acting. Retry delays grow from one minute to a maximum of 15 minutes. Fresh readings alone do not hide a task that has exited.

A maintenance lock serializes installation and watchdog recovery. Installation waits for the old sensor task to stop before replacing its library. Sample publication retries transient Windows replacement errors without damaging the previous sample; persistent errors back off and remain visible. Diagnostics record operation and error code, separately from the last successful reading.

Explicit thermal refresh checks the installed helper hash as well as fresh data. A running old helper is not treated as an installed upgrade. The Linux unit is published atomically, restarted explicitly, and checked for readiness. It uses a 30-second systemd watchdog, 10-second failure restart, and no core dumps. Successful status/journal publication precedes each watchdog notification. A pass has a combined two-second manager-call budget and rotates jobs so one slow scope does not starve all later jobs.

Setup replaces helper files atomically. Doctor repair can refresh a recognized stale annotator while preserving its CLI options; it verifies the installed script, UID, held lock and stable process handle before signaling that helper. It does not restart provider sessions.

```bash
codex-thermal install-service
codex-doctor --fix
codex-thermal status
```

Operator masks are never removed. A failed preview or rejected elevation is not permission to reinstall, unmask, or broaden enforcement.

## Enroll heavy work explicitly

Agent wrapping preserves interactive terminal behavior and immediate admission. It does not automatically turn all child shell commands into managed batches. Use exact argv for builds, tests, browsers and rendering:

```bash
codex-job run --role batch --scope required -- python3 -m unittest discover -s tests -v
```

For a separately chosen batch profile, for example:

```bash
codex-job run --role batch --scope required \
  --cpu-quota 150 --max-parallel 2 --admission-group builds \
  --reserve-memory-mib 2048 --memory-high 2048 --memory-max 3072 \
  -- cmake --build build
```

These flags select this job's policy. CPU and memory ceilings require a real enforcing scope and fail before execution if it is unavailable. A hard memory limit can cause that batch to be OOM-killed. No ceiling, pause, OOM-kill remedy or restart permission is accepted for interactive agents.

Concurrency and memory reservations are committed under one private lock before launch. `--max-parallel` groups this recipe by default; `--admission-group` selects a named cohort. Stronger active concurrency choices are retained. Reservations remain until recorded owners/payloads are dead, including orphaned or temporarily unreadable identities. Memory reservations are conservative admission promises, not allocations or hard limits. Unknown counters remain advisory for ordinary memory admission; an explicitly requested reservation waits for valid counters. Unregistered work can still consume RAM after admission.

Thermal enrollment additionally reserves capacity in the guard's bounded 64-job remedy journal before launching, so simultaneous admissions cannot overfill it. It does not impose a host task limit. Missing sensors, missing/incompatible guard heartbeats, insufficient guard capacity and unavailable enforcing scopes hold or refuse enrolled batch work. Queue timeouts report the actual reason. `--memory-policy ignore` does not bypass thermal protection or explicit reservations.

CPU limits apply only to identity-verified registered batches in this user/distro. Unregistered commands, native Windows work, other WSL distros and GPU loads are outside those Linux quotas. Include the launcher rule in each participating repository's agent instructions or recipes; no common tool is replaced with a hidden shim.

The default sensor-loss policy retains existing cooling quotas and holds new batches. It does not newly limit uncapped work already running. To explicitly select the configured batch CPU budget after 30 seconds of lost telemetry:

```bash
codex-thermal configure --sensor-file /mnt/c/ProgramData/CodexFarm/Thermal/temperature.json \
  --enforce --sensor-loss-policy budget --sensor-loss-grace-seconds 30
```

Configuration prints its concrete values before saving. It preserves existing numeric thresholds. The guard writes rollback state before a quota change, rechecks identity before every mutation, retains unknown records, and never restores over a later operator limit. `rollback_records` counts records needing reconciliation, not proof that all those jobs remain alive. Disabling enforcement allows the running guard to release its own unchanged quotas; `release` is also available when the monitor is stopped.

## Observe incidents without recording conversations

Aggregate history is off by default. To retain up to 24 hours with a 1 MiB byte cap:

```bash
codex-thermal configure --sensor-file /mnt/c/ProgramData/CodexFarm/Thermal/temperature.json \
  --enforce --history-hours 24 --history-max-bytes 1048576
```

Normal samples are downsampled to one minute; transitions are retained. The byte cap may retain less than the requested time span. The private history contains CPU temperatures, sample age, guest CPU utilization, memory headroom/pressure, guard state and aggregate cooling counts. It excludes argv, cwd, environment values, process identities and conversations. History failure preserves its old file, backs off retries, and does not terminate enforcement. Linux `/proc` counters describe the WSL guest, not every native Windows application's RAM/CPU use.

Sensor/guard/helper receipts distinguish acquisition failures, dead helpers, incompatible upgrades and failed remedies. They do not diagnose fans, thermal paste, GPU temperatures or firmware. Investigate physical cooling if thermal shutdowns recur; raising thresholds is not a substitute for that diagnosis.

## Recover exact conversations

Five-minute autosave publishes exact manifests and a separate `manifest.tsv.capture.json` heartbeat even when manifest bytes have not changed. Partial saves retain earlier recovery identities and report incomplete coverage. The capture digest detects a changed manifest or an interrupted publication; doctor no longer relies solely on file modification time.

Restore uses a stable normalized manifest under the same lock, skips exact conversations already active elsewhere, and never guesses the latest session. Its private `.restore.json` receipt records bounded row indices, identity hashes and outcomes, attempt number, manifest generation and completion status. Raw labels, paths and session IDs are omitted. Large attempts report receipt truncation instead of limiting the number of windows restored.

Autorestore has at most three starts per five-minute window, with a 15-second delay after failure. Successful detached tmux windows survive a partial failure. Retry uses the existing exact-identity checks and does not create a second conversation writer. Refresh existing unit definitions explicitly, preserving the independent archive choice:

```bash
codex-add --install-autoservice
```

The Windows sensor task starts at Windows boot. Linux guard/autorestore services start with the WSL user manager. Enabling those Linux units does not itself launch WSL after a cold Windows boot. This implementation does not silently add a Windows task to keep a distro running.

Manifest history is content-addressed. Optional retention removes only owned, unchanged snapshots whose filename digest matches their contents; it preserves the selected manifest's snapshot and unknown/operator files:

```bash
codex-manifest.py prune-history ~/.config/codexfarm/manifest.tsv --keep 100
# Opt in to the same retention after successful automatic captures:
CODEXFARM_MANIFEST_HISTORY_KEEP=100 codex-add --install-autoservice
```

Retention defaults off. It never deletes conversation archives or workspace files. Invalid automatic retention fails before replacing the manifest. Conversation resume recreates a CLI context; it does not resume process memory, replay interrupted commands or submit a continuation prompt.

## Preserve conversation data and workspace files separately

Full conversation archives remain a separate explicit choice:

```bash
codex-backup --archive --skip-save
codex-add --install-autoservice --with-conversation-backups
```

The archive budget defaults to 512 MiB of source and compressed output, with a 1 GiB disk reserve. SQLite databases are copied coherently with committed WAL data. Completed archives get a private per-archive SHA-256 receipt. File and directory publication is flushed; the complete new inventory and database contents are verified in a private temporary tree before updating `latest.json` or pruning a prior archive. A checksum receipt provides integrity relative to that trusted receipt, not an independent authenticity signature.

Verification rejects traversal, duplicate names, symbolic/hard links, special files, inventory mismatches, corrupt databases, and compressed/uncompressed/member/time-budget violations. Extraction accepts only a new private destination and never replaces a live provider home:

```bash
codex-recovery verify /selected/backup/snapshot-example.tar.gz
codex-recovery extract /selected/backup/snapshot-example.tar.gz \
  --destination /selected/new-private-restore
```

For an older archive without a retained checksum receipt, supply a trusted `--sha256` value. The default verifier limits are 20,000 members, 8 MiB inventory, 512 MiB data and 60 seconds; `--max-mib` explicitly raises the data budget when needed.

Optional copies require a selected destination:

```bash
codex-backup --archive --copy-to /selected/backup-device/farm --copy-keep 3
# Or copy an already completed archive:
codex-recovery copy /selected/backup/snapshot-example.tar.gz \
  --destination /selected/backup-device/farm --keep 3
```

Copies are verified, flushed, privately published without overwriting an operator file, and indexed for retention. Modified or unknown files are preserved. Local and copy success are reported separately; a failed destination does not invalidate the completed local archive. The tool does not select a destination, upload to a cloud service, or manage encryption keys. Choose an encrypted destination/transport for private conversations. `different_filesystem` is informational and does not prove a different physical disk or an off-machine copy.

When thermal enforcement is selected, full archive work runs through a required managed batch scope. Lightweight manifest capture happens first, before waiting for admission. Thus a stalled sensor or archive queue does not prevent that invocation from saving exact session identities. Archive choice remains independent of resource settings and memory/thermal setup.

Workspace snapshots are another explicit action. They preserve current tracked regular files and exact selected untracked paths without committing or pushing changes:

```bash
codex-recovery workspace --repo /selected/repository \
  --include-untracked notes/new-work.txt \
  --destination /selected/private-backups/workspace-example.tar.gz
```

Known credential/key paths, `.git`, provider homes, dependency caches and unselected untracked files are excluded. Symlink/special-file selections are rejected; directory descriptors prevent escaping the selected repository through path replacement. Changed files fail before publication. The verified archive is a file snapshot, not an atomic snapshot of the whole filesystem or unsaved editor buffers. Review exclusions and restore into a new directory before applying recovered work.

Heavy archive, copy, workspace and verification commands should use the batch launcher when invoked independently. Scheduled copies can use an explicitly selected `codex-backup --archive --copy-to ... --watch`; installing ordinary autoservice does not invent or persist an external destination.

## Validation and rollout

Automated tests use private HOME/XDG/proc trees, synthetic temperatures/clocks, loopback provider doubles and manager/task doubles. They never install drivers, change real services/tmux sessions, log in to a model, or deliberately overheat hardware. Windows CI runs native file-lock/replacement/retry and watchdog-decision self-tests plus syntax parsing without accessing hardware/tasks. Heavy local suites use the managed launcher.

Release validation still needs a separate supported Windows/WSL installation for a clean UAC/driver path, reboot, suspend/resume, battery/AC changes, unsupported sensors and operator-disabled components. Those checks cannot be inferred from mocks or a healthy already-configured host. Controlled helper restart and isolated recovery drills are preferable to forcing a crash on a working farm. An archive that was deliberately disabled, an unselected external destination or a repository that does not enroll heavy work must be reported as that boundary, not silently enabled.

Primary platform contracts: [systemd service watchdogs](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html), [sd_notify](https://www.freedesktop.org/software/systemd/man/latest/sd_notify.html), [Windows file-sharing rules](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew), and [Task Scheduler triggers](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtasktrigger).
