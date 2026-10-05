# Gentle Resource Protection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Protect interactive agents from competing memory use while keeping resource defaults gentle, and add bounded incident diagnosis with narrow managed-job remedies.

**Architecture:** Fixed advisory headroom and a pure admission gate feed an explicit managed-job runner. A separately requested root helper provides reclaim/OOM preference, while a detached, unprivileged incident worker uses a remote model and a deterministic action allowlist.

**Tech Stack:** Python 3.10+ standard library, Bash 3.2+, tmux, optional Linux cgroup v2/systemd user scopes and Codex CLI.

The approved spec is `docs/superpowers/specs/2026-10-05-resource-protection-design.md`.
The starting commit is upstream `cc15bc2`. Baseline: 568 unit tests passed.

## Task 1: Fixed headroom, compatible health checks, and admission gate

**Files:** Create `codex_looper/resource_policy.py`, `tests/test_resource_policy.py`;
modify `codex_looper/health.py`, `tests/test_health.py`, `README.md`.

- [x] Write failing tests for fixed default thresholds on 4/8/64 GiB hosts, exact
  1024/1536 boundaries, percentage compatibility, invalid/unknown counters, and
  swap/stall behavior. A representative assertion is:

```python
def test_default_headroom_does_not_scale_with_ram(self):
    for total in (4096, 8192, 65536):
        self.assertEqual(Memory(total, 1400, 0).level(Limits()), "warning")
        self.assertEqual(Memory(total, 1024, 0).level(Limits()), "critical")
        self.assertEqual(Memory(total, 2048, 0).level(Limits()), "ok")
```

- [x] Run `python3 -m unittest discover -s tests -p 'test_resource_policy.py' -v`
  and `python3 -m unittest discover -s tests -p 'test_health.py' -v`; confirm
  feature assertions fail before implementation.
- [x] Implement validated `HeadroomSettings` and `HeadroomGate`. The gate API is
  `sample(memory, now=<monotonic seconds>) -> bool`, where true means admission
  is allowed. Enter waiting at <=1024 MiB or critical memory stalls; exit after
  >=1536 MiB and below-warning stalls continuously for 30 seconds. Unknown
  counters and total <=1536 MiB return true with an advisory reason. Expose a
  human-readable `reason` and never issue process operations.
- [x] Preserve the existing `Limits` constructor compatibility and restore exit
  contracts. Add fixed MiB fields/policy, explicit MiB overrides, and preserve
  legacy explicit percentage variables. Append I/O pressure as a separate
  optional memory-reading field without making I/O stalls a RAM restore refusal.
- [x] Test recovery resets, clock rollback, configuration validation, and
  thresholds that do not fit a tiny host. Verify critical advisory/enforced
  behavior and shared-server accounting regressions remain covered.
- [x] Update the health README, run the affected suites and pinned Ruff, commit,
  and complete spec and quality reviews before Task 2.

## Task 2: Managed scopes, job identity, and explicit host protection

Deliver this task in two sequential reviewed parts: 2A implements managed jobs,
private settings, and launch integration; 2B implements the standalone host helper.
Neither part runs privileged changes during development.

Task 2A passed specification and quality review at `dab04e9`; 67 affected
tests passed. A short-job smoke check against the actual user manager verified
argv, live identity, process groups, priorities, hierarchy, and unlimited caps.

Task 2B passed specification and quality review at `7b94a0f`; all 50 host-helper
tests pass. Review fixes cover inactive-slice restoration, pending OOM-write
recovery and operator edits, and independent rollback after subprocess timeouts.
Unresolved timer stops retain their definition for safe retry. Privileged
activation has not been run.

**Files:** Create `bin/codex-job`, `codex_looper/resource_jobs.py`,
`codex_looper/resource_config.py`, `tests/test_resource_config.py`,
`bin/codex-resource-host`, `tests/test_resource_jobs.py`,
`tests/test_resource_host.py`; modify `bin/codex-add`, `tests/test_add_scripts.py`,
`codex_looper/process.py`, `tests/test_looper.py`, `pyproject.toml`, `README.md`, `AGENTS.md`.

- [x] Add failing CLI/process tests with private homes and systemd doubles.
  Verify argv preservation, agent bypass, optional queue timeout/manual bypass,
  generated scope identities, no implicit ceilings, batch priority/OOM
  preference, missing manager fallback, and mandatory explicit-limit failure.
  This public CLI contract is exercised without a live user manager:

```python
result = subprocess.run(
    [str(ROOT / "bin/codex-job"), "run", "--role", "batch", "--scope", "off",
     "--memory-policy", "ignore", "--", sys.executable, "-c",
     "import sys; print(sys.argv[1])", "literal $value; `data`"],
    env=private_env, text=True, capture_output=True, check=False,
)
self.assertEqual(result.returncode, 0)
self.assertEqual(result.stdout.strip(), "literal $value; `data`")
```

- [x] Run the new suites, observe meaningful failures, and implement the runner.
  Use shared parent `codexfarm.slice`, separate `codexfarm-interactive.slice` and `codexfarm-batch.slice`, generated
  `codexfarm-agent-<hex>.scope` / `codexfarm-batch-<hex>.scope` units, and argv-based
  subprocesses. Batch CPU/I/O weights are 25, Nice is 10, and OOM score is +250;
  agents have no imposed ceilings. Root-side agent OOM preference is -250.
  Agent scopes pass through their shared parent protection with MemoryLow=infinity.
  Derive a missing user-bus environment only from an owned `/run/user/<uid>`
  directory/socket; preserve explicitly configured bus environments.
- [x] Implement private job records and strict identity validation using PID
  start ticks, UID, scope generation and actual cgroup membership. Accept
  `--restartable` only for batch jobs, with one restart maximum and graceful
  termination limited to the exact owned job. A declared worker environment
  variable supports bounded future worker reductions. Never signal unrelated
  processes or select by process name.
- [x] Add host-helper tests for plan-only/no root writes, explicit apply,
  preserved stronger MemoryLow, parent hierarchy, root-owned installed helper,
  masked unit refusal, idempotence, failures/rollback, stale PID and wrong UID
  rejection, removal preserving operator edits, and unchanged retired guard and
  autosave masks.
- [x] Implement a standalone host helper with `plan`, `apply`, `maintain`, and
  `remove` subcommands, a root-owned configuration and narrow OOM maintenance
  timer. `plan --uid 1003` prints the complete requested change. `apply --uid
  1003 --with-maintenance` explicitly installs the requested 1 GiB protection and
  maintenance without restarting user sessions. Runtime and persistent settings
  retain backups and restoration.
- [x] Integrate agent wrapping only when `CODEXFARM_RESOURCE_PROTECTION=1` or
  private resource settings enable it. Trusted shell fragments continue to be
  interpreted once by the existing shell path; the runner receives their exact
  resulting argv. Apply the same optional wrapping to provider commands in the
  looper without altering provider argument construction or recovery identities.
  Normal setup remains privilege-free and creates no services.
- [x] Run affected suites and lint, document job/host commands and trust
  boundaries, commit, then complete both review stages.

## Task 3: Incident reports, remote diagnosis, authorized remedies, and rollout

**Files:** Create `bin/codex-resource`,
`codex_looper/resource_reports.py`, `codex_looper/resource_incidents.py`,
`codex_looper/resource_investigator.py`, `tests/test_resource_incidents.py`,
`tests/test_resource_investigator.py`; extend `codex_looper/resource_config.py`
and `tests/test_resource_config.py` as needed;
modify `codex_looper/health.py`, `codex_looper/resource_jobs.py`, `setup.sh`,
`tests/test_setup.py`, `README.md`, `AGENTS.md`, `pyproject.toml`.

- [ ] Test strict private configuration and current-report generation before
  implementation. The CLI is `codex-resource configure --protect-agents
  --queue-background --investigator codex --automatic-actions`; each setting has
  a disabling counterpart. `status`, `report --json`, and `investigate` are
  independent read/report commands. Configuration changes never alter services.
- [ ] Implement bounded proc/cgroup collection: <=4096 scanned PIDs, <=20 reported
  consumers, PSS for only top candidates where readable, two-second scan budget,
  and <=16 KiB model input. Include start identity, UID, memory/cgroup counters,
  pressure and swap context; exclude argv, environment values and conversations.
- [ ] Test sustained 60-second pressure, one-worker locking, 15-minute cooldown,
  low-memory deferral, process timeout/output limits, and bounded report retention.
  Integrate scheduling in the health monitor without blocking annotation.
- [ ] Test the Codex adapter's required isolation flags and unsupported-version
  failure. Implement ephemeral/read-only/schema-constrained diagnosis in a
  private HOME/CODEX_HOME/cwd, ignoring ordinary user config and disabling tool
  features supported by the CLI. Clear model-derived tooling in a private catalog
  selected from the bundled model catalog; flags alone are insufficient. Preserve
  existing CLI login with an authentication-only link, permitting native cache
  refresh while never copying credentials into reports or printing them. Test
  actual registered additional_tools and rejected execution/patch/collaboration
  dispatch through local fake transport, plus excluded user-context sentinels.
  Use a small fixed model instruction file and verify actual request input size,
  leaving room for native CLI framing within the 16 KiB input budget.
  A fake provider runs all deterministic tests without live authentication.
- [ ] Test model output type/schema validation, unknown actions, agent targets,
  stale identities, malicious telemetry labels, and oversized output. Implement
  a deterministic allowlist for job deferral, declared worker reduction, and a
  single restart request on a registered restartable batch job. Automatic
  actions require separate consent. Journal before/after metrics and bounded
  reversible-policy rollback; never execute generated shell commands.
- [ ] Verify setup installation parity and documentation. Run all required
  repository checks and two-stage review; address every material finding.
- [ ] Push a PR, wait for exact-head CI, merge to `main`, synchronize the local
  main checkout, and install helpers with `--without-session-hook`.
- [ ] Activate the approved user configuration, restart only the monitor if
  needed, verify healthy operation, backups remain off, old masks remain intact,
  and workload caps remain unlimited. Prepare the host apply command and its
  reviewed plan for the user's sudo password if privileged execution is unavailable.

## Required publication checks

Use the pinned Ruff virtualenv outside the repository. Run Bash syntax and
ShellCheck at warning severity for all tracked Bash scripts. Run:

```bash
CODEX_ANNOTATOR_AUTOSTART=0 python3 -m unittest discover -s tests -v
VALIDATE_SKIP_TMUX=1 ./validate.sh
./validate.sh
./examples/demo.sh
./tests/integration/session_resume_smoke.sh
CODEXFARM_DEEP_HISTORY_BIN=/tmp/agent-cli-farm-gentle-verify.1eQfY1/tmux-deep-history/bin/tmux-deep-history ./tests/integration/deep_history_smoke.sh
```

Expected: every command succeeds; test tools use private state/sockets and no
real memory exhaustion. The controller verifies CI and merged commit identities.
