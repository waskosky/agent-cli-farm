# Agent Looper Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a tmux-friendly prompt sequence looper for Codex and Claude that fits the existing Codex CLI Farm install and wrapper model.

**Architecture:** Implement the looper as a standard-library Python script in `bin/codex-looper.py`, with a small shell launcher in `bin/codex-looper`. Keep the existing `codex-add` tmux workflow as the owner of farm sessions, board linking, pipe-pane logs, and annotator startup; the looper itself runs prompt sequences and can optionally launch into a farm window via `codex-add`.

**Tech Stack:** Bash launchers, Python 3 standard library, `unittest`, existing `setup.sh` installer, existing tmux helper scripts.

### Task 1: Core Looper Behavior

**Files:**
- Create: `bin/codex-looper.py`
- Create: `bin/codex-looper`
- Test: `tests/test_looper.py`

**Step 1: Write failing tests**

Add tests for:
- prompt files split on separator lines containing only `---`
- Codex first/resume command construction
- Claude first/resume command construction
- stop-pattern parsing from stderr and structured Codex JSON
- `--dry-run --once` exits without running subprocesses

Run: `python3 -m unittest tests.test_looper`
Expected: FAIL because `bin/codex-looper.py` does not exist.

**Step 2: Implement minimal code**

Create `bin/codex-looper.py` with:
- dataclasses for agent/config/options
- prompt loading
- command template/building
- JSON/stderr stop parsing
- async subprocess runner with timeout
- CLI commands: `run`, `init`, `doctor`
- convenience defaults based on executable name and `CODEX_TOOL_NAME`

Create `bin/codex-looper` as a shell launcher that runs `python3 bin/codex-looper.py`.

**Step 3: Verify**

Run: `python3 -m unittest tests.test_looper`
Expected: PASS.

### Task 2: Farm Integration

**Files:**
- Modify: `bin/codex-looper.py`
- Test: `tests/test_looper.py`

**Step 1: Write failing tests**

Add tests for `--farm-session` launch behavior using a stub `codex-add`:
- `codex-looper --farm-session work --label sweep --cwd /repo --once --dry-run` re-execs through `codex-add`
- generated `CODEX_CMD`, `CODEX_NAME`, and `CODEX_ARGS` are correct
- tmux/farm-only args are removed before the inner looper command

Run: `python3 -m unittest tests.test_looper`
Expected: FAIL because farm launch is not implemented.

**Step 2: Implement minimal code**

Add `--farm-session`, `--farm-attach`, and `--farm-add-bin` options. When present outside an existing looper farm process, call `codex-add` with environment variables rather than raw `tmux`.

**Step 3: Verify**

Run: `python3 -m unittest tests.test_looper`
Expected: PASS.

### Task 3: Install Wrappers

**Files:**
- Modify: `setup.sh`
- Create or rely on generated: `bin/claude-looper`, `bin/gemini-looper`
- Test: `tests/test_setup.py`

**Step 1: Write failing tests**

Add setup tests asserting:
- `codex-looper` is copied
- `claude-looper` and `gemini-looper` are installed or generated

Run: `python3 -m unittest tests.test_setup`
Expected: FAIL because `looper` is not in wrapper suffixes yet.

**Step 2: Implement minimal code**

Add `looper` to `setup.sh` wrapper suffixes. Add checked-in wrapper files only if the current repo pattern requires explicit wrappers for user convenience.

**Step 3: Verify**

Run: `python3 -m unittest tests.test_setup`
Expected: PASS.

### Task 4: Annotator And Status Compatibility

**Files:**
- Modify: `bin/codex-annotator.py`
- Test: `tests/test_annotator_status.py`

**Step 1: Write failing tests**

Add a test that a pane with current command `python3` but start command containing `codex-looper` is classified as RUN.

Run: `python3 -m unittest tests.test_annotator_status`
Expected: FAIL because pane start command is not currently available to classification.

**Step 2: Implement minimal code**

Extend `PaneInfo` with `start_command`, include `#{pane_start_command}` from tmux, and classify looper panes as RUN while the process is alive.

**Step 3: Verify**

Run: `python3 -m unittest tests.test_annotator_status`
Expected: PASS.

### Task 5: Documentation And Ignore Rules

**Files:**
- Modify: `README.md`
- Modify: `.gitignore`
- Modify: `validate.sh`

**Step 1: Update docs**

Document:
- `codex-looper`, `claude-looper`, `gemini-looper`
- `codex-looper init`
- prompt separator behavior
- safe defaults and stop conditions
- farm launch example using `--farm-session`

**Step 2: Update validation**

Ensure `validate.sh` syntax-checks/compiles `bin/codex-looper.py` and includes the launcher in existing `bin/codex-*` checks.

**Step 3: Carry temp ignore**

Keep `/temp/` in `.gitignore` so the reference checkout stays untracked.

### Task 6: Full Verification And Release

**Files:**
- All changed files

**Step 1: Run full verification**

Run:
- `python3 -m unittest discover -s tests`
- `bash validate.sh`
- `bin/codex-looper --help`
- `bin/codex-looper init --force` in a temporary directory
- `bin/codex-looper --agent codex --dry-run --once --label smoke --prompt-file prompts.md` in that temporary directory

Expected: all pass.

**Step 2: Commit**

Run:
- `git status --short`
- `git add ...`
- `git commit -m "feat: add agent looper"`

**Step 3: Merge and push**

Run from main checkout:
- `git fetch origin`
- verify `main` has not diverged unexpectedly
- merge `feature/agent-looper`
- run the full verification commands again
- `git push origin main`
