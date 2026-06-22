# Looper Revamp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Implement the looper revamp described in `docs/plans/looper-revamp.md`: single-prompt endless mode by default, opt-in completion detection, plan gates, git backups, no-progress circuit breaker, and presets.

**Architecture:** Keep the current standard-library Python implementation in `bin/codex-looper.py`. Add small helper functions for prompt loading modes, completion detection, plan checklist state, git safety features, and preset loading. Preserve the current farm launcher and retry behavior.

**Tech Stack:** Python 3 standard library, `unittest`, bash wrapper scripts, existing docs in `README.md` and `docs/looper.md`.

---

### Task 1: Prompt Modes And Default Inference

**Files:**
- Modify: `tests/test_looper.py`
- Modify: `bin/codex-looper.py`
- Modify: `docs/looper.md`

- [x] **Step 1: Write failing tests**

Add tests that prove:
- `load_prompts_for_mode(path, separator, "single")` returns the entire file as one prompt and does not split `---`.
- `load_prompts_for_mode(path, separator, "sequence")` preserves the existing `---` split behavior.
- `resolve_prompt_defaults()` returns `mode="single"` and `PROMPT.md` when no config or CLI prompt is explicit.
- `resolve_prompt_defaults()` infers `sequence` with `prompts.md` when `prompts.md` exists, `PROMPT.md` does not, and mode was not explicit.

Run:

```bash
python3 -m unittest tests.test_looper.LooperCoreTests
```

Expected: FAIL because these helpers and fields do not exist yet.

- [x] **Step 2: Implement prompt mode support**

Add:
- `LooperMode = Literal["single", "sequence"]`
- `LooperConfig.mode`
- `DEFAULT_SINGLE_PROMPT_FILE = Path("PROMPT.md")`
- `DEFAULT_SEQUENCE_PROMPT_FILE = Path("prompts.md")`
- `load_prompts_for_mode()`
- `resolve_prompt_defaults()`
- `--mode {single,sequence}`

Update `run_loop()` to call the new prompt loader and print mode in its startup summary.

- [x] **Step 3: Verify prompt mode tests pass**

Run:

```bash
python3 -m unittest tests.test_looper.LooperCoreTests
```

Expected: PASS.

### Task 2: Completion Detection

**Files:**
- Modify: `tests/test_looper.py`
- Modify: `bin/codex-looper.py`
- Modify: `docs/looper.md`

- [x] **Step 1: Write failing tests**

Add tests that prove:
- A stdout line containing `EXIT_SIGNAL: true` sets `ProcessResult.completion_detected`.
- `run_loop()` stops after one completed loop when `completion_enabled=true`.
- `completion_streak=2` requires two consecutive completed loops with the completion marker.
- Missing completion markers reset the streak.

Run:

```bash
python3 -m unittest tests.test_looper.LooperCoreTests
```

Expected: FAIL because completion detection does not exist.

- [x] **Step 2: Implement completion support**

Add config and CLI:
- `completion_enabled=false`
- `completion_marker=r"EXIT_SIGNAL:\s*true"`
- `completion_streak=1`
- `--complete-on REGEX`
- `--completion-streak N`

Compile the completion marker only when enabled. Detect marker matches in stdout or stderr without treating them as stop-pattern failures. After a successful loop, increment/reset the completion streak and stop once it reaches the threshold.

- [x] **Step 3: Verify completion tests pass**

Run:

```bash
python3 -m unittest tests.test_looper.LooperCoreTests
```

Expected: PASS.

### Task 3: Plan File Gate

**Files:**
- Modify: `tests/test_looper.py`
- Modify: `bin/codex-looper.py`
- Modify: `docs/looper.md`

- [x] **Step 1: Write failing tests**

Add tests that prove:
- `markdown_plan_has_unchecked_tasks()` returns true for `- [x] task`.
- It returns false for checked tasks and ordinary text.
- A completion marker does not stop the looper while the configured plan file has unchecked tasks.
- The looper stops when the marker is present and the plan file has no unchecked tasks.

Run:

```bash
python3 -m unittest tests.test_looper.LooperCoreTests
```

Expected: FAIL because plan gating does not exist.

- [x] **Step 2: Implement plan file gate**

Add `plan_file=""` and `--plan-file PATH`. If configured, require the plan file to have no unchecked markdown checkboxes before completion streak can advance. If the marker appears while the plan is incomplete, print a clear line and continue looping.

- [x] **Step 3: Verify plan gate tests pass**

Run:

```bash
python3 -m unittest tests.test_looper.LooperCoreTests
```

Expected: PASS.

### Task 4: Git Backups And No-Progress Circuit Breaker

**Files:**
- Modify: `tests/test_looper.py`
- Modify: `bin/codex-looper.py`
- Modify: `docs/looper.md`

- [x] **Step 1: Write failing tests**

Add tests that prove:
- `create_backup_branch()` creates a backup branch named under the configured prefix.
- `prune_backup_branches()` keeps the newest configured branch count.
- `git_workspace_fingerprint()` changes when tracked content or HEAD changes.
- `run_loop()` stops after `cb_no_progress` loops that do not change the git fingerprint.

Run:

```bash
python3 -m unittest tests.test_looper.LooperCoreTests
```

Expected: FAIL because backup and circuit breaker helpers do not exist.

- [x] **Step 2: Implement safety helpers**

Add config and CLI:
- `backup_enabled=false`
- `backup_prefix="looper-backup"`
- `backup_keep=10`
- `--backup`
- `--backup-prefix PREFIX`
- `--backup-keep N`
- `cb_no_progress=0`
- `--cb-no-progress N`

Create a backup branch before each loop when enabled. Prune old backup branches lexicographically by timestamped branch name. Compare a git fingerprint from before and after each loop to count no-progress loops, then stop when the configured threshold is reached.

- [x] **Step 3: Verify safety tests pass**

Run:

```bash
python3 -m unittest tests.test_looper.LooperCoreTests
```

Expected: PASS.

### Task 5: Preset Loading

**Files:**
- Modify: `tests/test_looper.py`
- Modify: `bin/codex-looper.py`
- Create: `examples/presets/rai.toml`
- Modify: `docs/looper.md`

- [x] **Step 1: Write failing tests**

Add tests that prove:
- `--preset PATH` loads a TOML file and applies its `[looper]` and `[agents.*]` values.
- `--preset rai` resolves to the repo example preset.
- CLI flags override preset values.

Run:

```bash
python3 -m unittest tests.test_looper.LooperCliTests
```

Expected: FAIL because presets do not exist.

- [x] **Step 2: Implement preset loading**

Add `--preset NAME_OR_PATH`. Load config in this order: built-in defaults, project `agent-looper.toml`, preset TOML, CLI options. Resolve named presets from `examples/presets/<name>.toml` and `~/.config/codexfarm/presets/<name>.toml`.

- [x] **Step 3: Verify preset tests pass**

Run:

```bash
python3 -m unittest tests.test_looper.LooperCliTests
```

Expected: PASS.

### Task 6: Documentation And Full Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/looper.md`
- Modify: `docs/plans/looper-revamp.md`

- [x] **Step 1: Update docs**

Document:
- `PROMPT.md` single-mode default.
- Sequence compatibility through `mode="sequence"` and `prompts.md` inference.
- Completion marker contract.
- Plan file gate.
- Backup and circuit breaker options.
- Preset usage, including `--preset rai`.

- [x] **Step 2: Run full test suite**

Run:

```bash
python3 -m unittest discover -s tests
```

Expected: PASS.

- [x] **Step 3: Run a dry-run smoke test**

Run in a temp directory with `PROMPT.md`:

```bash
python3 /path/to/bin/codex-looper.py --local --once --dry-run
```

Expected: Uses `PROMPT.md`, mode `single`, and prints one command.

- [x] **Step 4: Commit**

Commit the implementation branch with:

```bash
git add bin/codex-looper.py tests/test_looper.py docs/looper.md README.md docs/plans/looper-revamp.md docs/superpowers/plans/2026-06-22-looper-revamp-implementation.md examples/presets/rai.toml
git commit -m "feat: revamp agent looper"
```
