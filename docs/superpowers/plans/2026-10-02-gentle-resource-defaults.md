# Gentle Resource Defaults Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make farm resource diagnostics advisory by default, separate archive consent, and honor deliberately disabled services without restricting normal work.

**Architecture:** Update the shared Bash commands and Python health helper; provider wrappers continue to delegate to them. Extend the existing subprocess tests with private state directories and synthetic memory/systemd/tmux inputs. Build on origin/main `8a2c570`, retaining its opt-in archive flag, size budget, disk reserve, and reduced scheduling priority.

**Tech Stack:** Bash 3.2, Python 3.10+, stdlib unittest, tmux, systemd user units, Ruff 0.12.2, ShellCheck.

---

## Approved context and boundaries

The user approved `docs/plans/2026-10-02-gentle-resource-defaults-design.md`
and explicitly requested push and merge to main. The privileged host guard is
not present in available farm Git history. Its timer/service are disabled and
masked, and its resource ceilings have already been removed. Production
autosave units must remain masked and `autoservice_choice` must remain `no`.
Use only temporary homes, state directories, providers, and tmux sockets for
tests. Do not run backups against production history or restart provider chats.

## Task 1: Implement and document the shared resource policy

This is one integrated task because service consent, archive health, restore
policy, and their user-facing descriptions must agree. One implementer owns
the following paths; the controller owns this plan and incident design.

**Files:**
- Modify: `bin/codex-add` — explicit autoservice install and separate archive consent.
- Modify: `bin/codex-restore` — advisory/default and explicit/enforced memory policy.
- Modify: `codex_looper/health.py` — restore-check policy and archive expectation.
- Modify: `bin/codex-annotator.py` — five-second default polling.
- Modify if required: `bin/codex-doctor` — respect deliberate autoservice disablement.
- Modify: `README.md`, `AGENTS.md` — defaults, migration, resource boundaries.
- Test: `tests/test_add_scripts.py`, `tests/test_health.py`, `tests/test_codex_annotator.py`, and `tests/test_doctor.py` when doctor behavior changes.

- [x] **Step 1: Add behavioral regressions and observe expected failures.**

Use existing unittest fixtures to run real commands with fake external tools.
Include critical memory with normal and forced restores, explicit enforcement
before mutation and between launches, ignored memory, unavailable/broken health
helpers, invalid environment policies, CLI overrides, and registered/provider
propagation. Update old blocking-memory tests to opt into enforcement.

Representative CLI assertions within the existing health subprocess fixture:

```python
result = subprocess.run(
    [str(ROOT / "bin/codex-health"), "--restore-check"],
    env=env, text=True, capture_output=True, check=False,
)
self.assertEqual(result.returncode, 0)
self.assertIn("critical", result.stdout.lower())
result = subprocess.run(
    [str(ROOT / "bin/codex-health"), "--restore-check", "--enforce-memory-pressure"],
    env=env, text=True, capture_output=True, check=False,
)
self.assertEqual(result.returncode, 3)
```

Service regressions must assert actual generated commands and external command
logs: manifest-only default; `--archive --min-age 3600` only after explicit
archive yes; separate choice persists across explicit refreshes and toggles
off; legacy autoservice yes alone does not request archives; normal stored-yes
launch registers its farm without writes/reload/enable/start; masks leave all
unit files unchanged, including local `/dev/null` links and manager-reported
runtime/global masks. Both backup flags require `--install-autoservice` and
conflicting flags fail before tmux mutation. Disabled archive health must ignore
stale status/watcher files; enabled archive scheduling still reports faults.

Run focused tests and confirm failures are behavioral, not fixture errors:

```bash
CODEX_ANNOTATOR_AUTOSTART=0 python3 -m unittest tests.test_health tests.test_add_scripts tests.test_codex_annotator -v
```

- [x] **Step 2: Implement advisory restore policy in the shared core.**

Replace `ignore_memory` with a validated policy string initialized from
`CODEXFARM_RESTORE_MEMORY_POLICY` (default `warn`). CLI
`--enforce-memory-pressure` sets `enforce`; the existing
`--ignore-memory-pressure` sets `ignore`; CLI wins over the environment.
Validate the final policy before any tmux mutation. Pass the chosen policy to
recursive registered restores using an explicit flag or scoped environment.

The core branching contract is:

```bash
case "$memory_policy" in
  warn|enforce|ignore) ;;
  *) echo "Invalid CODEXFARM_RESTORE_MEMORY_POLICY: $memory_policy" >&2; exit 2 ;;
esac
```

Ignore returns success without probing; warn runs advisory health, emits a
warning when it is unavailable or fails, and returns success; enforce invokes
`--restore-check --enforce-memory-pressure`, refuses a missing/broken helper,
and preserves the caller's exit 3. Preserve two-second pacing, exact identity
checks, existing locks, and checks before forced deletions and each launch.

Add the health CLI enforcement flag. It requires `--restore-check`; ordinary
health diagnostic exits stay unchanged. The return contract is:

```python
return 3 if args.enforce_memory_pressure and level == "critical" else 0
```

- [x] **Step 3: Implement autoservice consent and mask protection.**

Parse `--with-conversation-backups` / `--without-conversation-backups` as a
separate `yes` / `no` request, valid only with explicit autoservice install.
Reject conflicting flags. Store it at `$STATE_DIR/conversation_backup_choice`.
No separate choice means `no`, even for a legacy `autoservice_choice=yes`.
Preserve a previously stored separate choice during later explicit refreshes.

The generated command contract is:

```bash
autosave_command="$SAVE_BIN --all-registered"
if [ "$conversation_backup_choice" = yes ]; then
  autosave_command="$BACKUP_BIN --archive --min-age 3600"
fi
```

Persist choices only in appropriate installation/choice paths. An ordinary
launch with stored autoservice yes calls `register_autoservice_farm` and returns
without writing or starting units. Preserve first-time interactive/environment
consent installation and the restore-parent safeguard. Explicit install remains
the refresh path.

Before writing any unit, check all three relevant units for masks. Detect local
symlink-to-`/dev/null` masks without following writes; query the user's manager
for masks inherited from runtime/global definitions. If any unit is masked,
report its name, preserve files/choices, and fail explicit install. Do not
unmask or instruct automatic unmasking. Retain `Nice=10`, idle I/O priority, and
the existing three-minute helper timeout; add no agent/account quotas.

- [x] **Step 4: Align archive health and polling.**

Keep the explicit diagnostic environment override first. Otherwise, explicit
archive or autoservice `no` suppresses archive scheduler warnings. An explicit
archive yes with autoservice yes means scheduled archives are expected; the
upstream archive watcher marker remains the fallback signal for other setups.

```python
if os.environ.get("CODEXFARM_BACKUP_HEALTH_ENABLED", "0") == "1":
    return True
```

Do not inspect, copy, delete, or prune old archives just to disable alerts.
Change the annotator fallback to `5.0`; retain positive finite validation and
explicit environment/CLI overrides. If doctor reports a deliberately disabled
timer as a fault, make it informational and add a behavior regression.

- [x] **Step 5: Update guidance and run focused verification.**

README and CLI help must describe advisory restore defaults, enforce/ignore
flags, `CODEXFARM_RESTORE_MEMORY_POLICY`, separate persisted archive choice,
explicit refresh, preserved masks, lightweight autosaves, and the five-second
annotator default. Preserve upstream archive size/disk safeguards. Add to
AGENTS: normal setup/operation must not install privileged host guards, replace
ordinary tools, or impose global CPU/RAM/swap/task/time limits; any requested
enforcement must be explicit, job-scoped, and show the proposed settings.

Run the focused tests from Step 1 plus doctor/backup modules, Bash syntax,
ShellCheck on changed shell commands, and pinned Ruff. Report observed red/green
test evidence, exact files, and any concerns. Self-review, stage only owned
paths, and commit the implementation.

- [x] **Step 6: Independent spec review, then quality review.**

The controller dispatches a fresh spec reviewer against the full approved
requirements and actual source. Address every gap and repeat review. Only after
spec approval, dispatch the quality reviewer against exact base/head SHAs.
Address important defects and repeat review. Include the approved incident
design in the final publication.

## Task 2: Verify, publish, merge, and update installed helpers

Prepublication checkpoint: the full 525-test suite passed at `09adc1e`; after
the installer-only quality fixes, all 44 affected add tests passed at
`11fed15`. All listed static and isolated integration checks passed. Spec and
quality reviews approved `11fed15`. Publication CI reruns the complete suite
on the final commit. The remaining checklist records the publication handoff.

The controller executes this operational task; no production services are
activated. User approval to publish and merge is already explicit.

- [x] Run pinned Ruff format/check, Bash syntax and ShellCheck across tracked scripts.
- [x] Run full unittest discovery with annotator autostart disabled.
- [x] Run `VALIDATE_SKIP_TMUX=1 ./validate.sh`, normal isolated validation, demo, and exact session resume integration.
- [x] Install the checksum-pinned deep-history backend in a temporary destination and run its isolated integration.
- [ ] Have a final reviewer confirm the complete change is ready to merge.
- [ ] Fetch current origin/main, resolve any new integration changes, and rerun checks affected by any edits.
- [ ] Push the branch, create a PR, wait for both CI Python versions, and merge into origin/main.
- [ ] Fast-forward local main to origin/main; copy installed helpers with `bash setup.sh --without-session-hook`.
- [ ] Verify installed helper freshness, five-second default, masks and `autoservice_choice=no`, no guard wrappers, and no restored resource caps. Do not restart provider chats.
- [ ] Report the merged PR, root cause, checks, and remaining installer attribution uncertainty.
