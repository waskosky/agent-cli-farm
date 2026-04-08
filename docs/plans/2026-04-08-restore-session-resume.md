# Restore Session Resume Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make restore recreate each saved window and then run the correct tool-specific resume command inside that window for Codex, Claude, and Gemini.

**Architecture:** Keep restore orchestration in `bin/codex-restore` and derive the resume command from the active tool name. Reuse the existing `codex-add` path to create windows, then target the newly created tmux window with `send-keys` so only restored panes receive the resume command.

**Tech Stack:** Bash entrypoints, tmux CLI, Python `unittest`/`pytest`.

---

### Task 1: Add failing restore tests

**Files:**
- Modify: `tests/test_add_scripts.py`

**Step 1: Write the failing test**

Add tests that run `bin/codex-restore` against a stub tmux binary and assert:
- restored Codex windows receive `send-keys -t <session>:<window> codex resume --last C-m`
- restored Claude windows receive `send-keys -t <session>:<window> claude --continue C-m`
- restored Gemini windows receive `send-keys -t <session>:<window> gemini --resume latest C-m`

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest -q tests/test_add_scripts.py -k restore`
Expected: FAIL because `bin/codex-restore` currently recreates windows but does not send any resume command.

### Task 2: Implement restore-time resume injection

**Files:**
- Modify: `bin/codex-restore`

**Step 1: Write minimal implementation**

Update restore to:
- resolve the active tool from `CODEX_TOOL_NAME`
- map that tool to its resume command
- capture the tmux window index returned by `codex-add`
- send the mapped command into the new window with `tmux send-keys`

**Step 2: Run test to verify it passes**

Run: `python3 -m pytest -q tests/test_add_scripts.py -k restore`
Expected: PASS with the new `send-keys` calls recorded for each tool mode.

### Task 3: Verify surrounding behavior and docs

**Files:**
- Modify: `README.md`

**Step 1: Update the user-facing restore description**

Document that restored Codex, Claude, and Gemini windows automatically issue their respective resume command after the pane is recreated.

**Step 2: Run the targeted regression suite**

Run: `python3 -m pytest -q tests/test_add_scripts.py tests/test_claude_wrappers.py`
Expected: PASS with restore coverage added and wrapper behavior unchanged.
