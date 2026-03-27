# Friendly Farms and Gemini Wrappers Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add human-friendly named farm selection to the core tmux commands and ship `gemini-*` wrappers alongside the existing `claude-*` wrappers.

**Architecture:** Extend the Bash entrypoints so `codex-add`, `codex-board`, and `codex-resume` can resolve a named farm from explicit flags or friendly positional arguments without requiring `CODEX_SESSION`. Derive each board session from the selected farm name, ship the wrapper scripts directly in `bin/`, and update setup/docs so installed helpers stay in sync.

**Tech Stack:** Bash entrypoints, Python `unittest`/`pytest`, tmux CLI wrappers, README/setup documentation.

### Task 1: Add failing tests for named farms and wrapper delivery

**Files:**
- Modify: `tests/test_add_scripts.py`
- Modify: `tests/test_claude_wrappers.py`

**Step 1: Write the failing tests**

Add coverage for:
- `codex-add worktree-name /path` using `worktree-name` as the tmux session instead of requiring `CODEX_SESSION`
- `codex-add worktree-name` using the current directory with the named session
- `codex-resume worktree-name` and `codex-resume worktree-name --board`
- wrapper existence checks for both `claude-*` and `gemini-*`
- `gemini-add` invoking the `gemini` executable

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest -q tests/test_add_scripts.py tests/test_claude_wrappers.py`

Expected: FAIL because the current scripts do not support friendly farm positional parsing and the repo does not ship wrapper scripts.

**Step 3: Commit**

```bash
git add tests/test_add_scripts.py tests/test_claude_wrappers.py
git commit -m "test: cover named farms and gemini wrappers"
```

### Task 2: Implement named farm parsing and board naming

**Files:**
- Modify: `bin/codex-add`
- Modify: `bin/codex-board`
- Modify: `bin/codex-resume`

**Step 1: Write the minimal implementation**

Implement a shared parsing model in each script:
- explicit `--session <name>` support
- friendly positional handling for `codex-add`:
  - one positional existing path => directory in default farm
  - one positional non-path => farm name using current directory
  - two positionals => farm name + directory
- friendly positional handling for `codex-resume` and `codex-board`:
  - optional farm name positional
  - `--board` on resume selects `<farm>-board`
- derive board session names as `<farm>-board` instead of a single global `board`

**Step 2: Run tests to verify the implementation**

Run: `python3 -m pytest -q tests/test_add_scripts.py`

Expected: PASS for the newly added parsing and routing tests.

**Step 3: Commit**

```bash
git add bin/codex-add bin/codex-board bin/codex-resume tests/test_add_scripts.py
git commit -m "feat: add friendly named farm commands"
```

### Task 3: Ship wrapper scripts for Claude and Gemini

**Files:**
- Create: `bin/claude-add`
- Create: `bin/claude-annotator`
- Create: `bin/claude-board`
- Create: `bin/claude-restore`
- Create: `bin/claude-resume`
- Create: `bin/claude-save`
- Create: `bin/claude-status`
- Create: `bin/claude-watch`
- Create: `bin/gemini-add`
- Create: `bin/gemini-annotator`
- Create: `bin/gemini-board`
- Create: `bin/gemini-restore`
- Create: `bin/gemini-resume`
- Create: `bin/gemini-save`
- Create: `bin/gemini-status`
- Create: `bin/gemini-watch`
- Modify: `tests/test_claude_wrappers.py`

**Step 1: Write the minimal implementation**

Add direct executable wrapper scripts in `bin/` that set `CODEX_TOOL_NAME` and `exec` the corresponding `codex-*` command from the same directory. Update the wrapper existence test so it covers both Claude and Gemini wrapper sets.

**Step 2: Run tests to verify the implementation**

Run: `python3 -m pytest -q tests/test_claude_wrappers.py`

Expected: PASS with all shipped wrappers present and executable.

**Step 3: Commit**

```bash
git add bin/claude-* bin/gemini-* tests/test_claude_wrappers.py
git commit -m "feat: ship claude and gemini wrappers"
```

### Task 4: Update setup and docs

**Files:**
- Modify: `setup.sh`
- Modify: `README.md`

**Step 1: Write the minimal implementation**

Update setup so it copies or generates both Claude and Gemini wrappers consistently, and update README examples/command listings to document:
- friendly named farm usage
- derived board names
- `gemini-*` wrappers alongside `claude-*`

**Step 2: Run tests to verify the implementation**

Run: `python3 -m pytest -q tests/test_setup.py`

Expected: PASS with setup still installing the expected helper scripts.

**Step 3: Commit**

```bash
git add setup.sh README.md tests/test_setup.py
git commit -m "docs: document named farms and gemini wrappers"
```

### Task 5: Full verification

**Files:**
- No code changes expected

**Step 1: Run the targeted and full verification suite**

Run:
- `python3 -m pytest -q tests/test_add_scripts.py tests/test_claude_wrappers.py tests/test_setup.py`
- `python3 -m pytest -q`

Expected: PASS with wrapper-related baseline failures resolved.

**Step 2: Record any residual risk**

If needed, note that shell-script behavior around ambiguous single positional arguments still depends on path existence, and document the explicit `--session` flag as the escape hatch.
