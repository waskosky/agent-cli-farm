# Looper Module Extraction Implementation Plan

**Goal:** Start splitting `bin/codex-looper.py` into importable modules without changing CLI behavior.

**Architecture:** Keep `bin/codex-looper.py` as the executable compatibility facade. Move stable constants, dataclasses, errors, formatting helpers, prompt helpers, git helpers, and TOML/config loading into a new `codex_looper/` package. Re-export moved symbols from the facade so existing tests and external import users keep working.

**Tech Stack:** Python 3.10+, stdlib dataclasses/argparse/asyncio/subprocess, existing `unittest` suite, Ruff, ShellCheck.

### Task 1: Add Package Import Smoke Tests

**Files:**
- Modify: `tests/test_looper.py`
- Create: `codex_looper/__init__.py`

**Step 1: Write failing tests**

Add tests proving the package can be imported directly and that the executable facade still exposes `load_config`, `LooperConfig`, and `run_command_main`.

**Step 2: Run tests and verify failure**

Run: `CODEX_ANNOTATOR_AUTOSTART=0 python3 -m unittest tests.test_looper.LooperCoreTests.test_looper_package_exports_core_symbols -v`

Expected: FAIL because `codex_looper` does not exist yet.

**Step 3: Add minimal package shell**

Create `codex_looper/__init__.py` and re-export symbols after extraction tasks.

### Task 2: Extract Models And Prompt Helpers

**Files:**
- Create: `codex_looper/models.py`
- Create: `codex_looper/prompts.py`
- Modify: `bin/codex-looper.py`

**Steps:**
1. Move `ConfigError`, `PromptError`, `CommandTemplateError`, dataclasses, literals, and constants to `models.py`.
2. Move prompt loading/default resolution to `prompts.py`.
3. Import/re-export these from `bin/codex-looper.py`.
4. Run focused prompt/config tests.

### Task 3: Extract Git, Retry, Process, Tmux, And Config Helpers

**Files:**
- Create: `codex_looper/git_safety.py`
- Create: `codex_looper/retry.py`
- Create: `codex_looper/process.py`
- Create: `codex_looper/tmux.py`
- Create: `codex_looper/config.py`
- Modify: `bin/codex-looper.py`

**Steps:**
1. Move related helper functions module by module.
2. Keep `bin/codex-looper.py` responsible for CLI parsing, init guidance, farm launch, and `main()`.
3. Re-export all moved functions used by tests.
4. Run `tests.test_looper` after each extraction group.

### Task 4: Verify And Publish

**Files:**
- Modify as needed from prior tasks.

**Steps:**
1. Run Ruff format/check.
2. Run Bash syntax and ShellCheck.
3. Run full unit suite and validate scripts.
4. Merge to `main` and push `origin/main`.
