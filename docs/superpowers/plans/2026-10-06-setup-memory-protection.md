# Setup Memory Protection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Offer an informed memory-protection choice during setup while keeping unattended installs inert and host changes separately consented.

**Architecture:** A small Python setup controller reuses existing validated resource settings and the standalone host helper. Bash handles flags and installation; resource policy and privileged helper behavior are unchanged.

**Tech Stack:** Python 3.10+ stdlib, Bash 3.2+, unittest, Ruff 0.12.2, ShellCheck.

## Task 1: Setup choice, host preview, integration, and documentation

**Files:**
- Create `codex_looper/resource_setup.py` and `bin/codex-setup-memory.py`.
- Create `tests/test_resource_setup.py`; extend `tests/test_setup.py`.
- Modify `setup.sh`, `README.md`, and `AGENTS.md`.

- [ ] Write meaningful failing controller tests before production code. Cover
  the full behavior specified in the design, with private settings and mocked
  host subprocesses. For example, explicit unattended enable must write only
  the two user preferences and must never run a host subprocess:

  ```python
  before = ResourceSettings(investigator="codex", automatic_actions=True)
  write_settings(before)
  result = controller("enable", input_stream, output_stream)
  self.assertEqual(result, 0)
  after = load_settings()
  self.assertTrue(after.protect_agents)
  self.assertTrue(after.queue_background)
  self.assertEqual(after.investigator, before.investigator)
  self.assertEqual(after.automatic_actions, before.automatic_actions)
  host_run.assert_not_called()
  ```

- [ ] Run `python3 -m unittest tests.test_resource_setup -v`; confirm failures
  identify the absent feature before implementing it.
- [ ] Implement the controller and entry point. Use literal argv for read-only
  preview and the separately accepted sudo apply, preserve all other settings,
  and return honest partial-failure status. Keep helpers small and focused.
- [ ] Extend real setup tests before changing Bash. Verify no settings on a
  normal piped run, explicit opt-in with private persisted settings, explicit
  skip retaining prior choices, conflicting flags failing before writes, and
  actual terminal prompting. Use private helper/sudo doubles for host paths.
- [ ] Add `--with-memory-protection` and `--without-memory-protection` flags to
  setup help and invoke the controller after copied helpers/package are ready.
  Preserve sourced setup's shell options/functions and exact flag forwarding.
- [ ] Update quick-start/resource documentation and maintenance policy to
  describe interactive default no, unattended behavior, preserved settings,
  tradeoffs, separate sudo preview/consent, and separately controlled AI options.
- [ ] Run `python3 -m unittest tests.test_resource_setup tests.test_setup
  tests.test_resource_config tests.test_resource_host -v`, pinned Ruff format
  and lint, Bash syntax, ShellCheck, and diff checks. Commit and self-review.
- [ ] Complete specification review followed by quality/integration review;
  address every material finding before publication.

## Publication and local refresh

- [ ] Run all required repository unit and integration checks with private
  fixtures, including the existing native isolation matrix where available.
- [ ] Push a PR and wait for successful exact-head Python 3.10/3.13 CI. Merge
  with the reviewed head guard and synchronize the clean local main checkout.
- [ ] Install from merged main using
  `bash setup.sh --without-session-hook --without-memory-protection` so this
  maintenance refresh preserves live settings and creates no new host changes.
- [ ] Verify installed entry-point/module parity, current settings preservation,
  old guard/autosave masks, healthy monitor, and clean main/origin synchronization.
