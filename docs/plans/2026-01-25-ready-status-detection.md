# Ready Status Detection Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Improve READY/RUN/ERR detection for Codex and Claude tmux panes by parsing prompt output, mirroring cli-agent-orchestrator heuristics.

**Architecture:** Add prompt-aware classification helpers in `bin/codex-annotator` that capture recent tmux output for Codex/Claude panes and map it to READY/RUN/ERR. Aggregate pane states to window state, keeping existing fallback behavior for non-Codex/Claude panes.

**Tech Stack:** Python 3 script (`bin/codex-annotator`), tmux capture-pane, unittest (stdlib) for tests.

### Task 1: Add prompt-aware detection + window aggregation

**Files:**
- Modify: `bin/codex-annotator`
- Create: `tests/test_annotator_status.py`

**Step 1: Write the failing test**

```python
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
import sys
import unittest


def load_annotator_module():
    annotator_path = Path(__file__).resolve().parents[1] / "bin" / "codex-annotator"
    loader = SourceFileLoader("codex_annotator", str(annotator_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


class AnnotatorStatusTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_annotator_module()

    def test_codex_ready_at_prompt(self):
        output = "Hello\n\u276f\n"
        self.assertEqual(self.mod.classify_codex_output(output), "READY")

    def test_codex_ready_on_waiting_prompt(self):
        output = "Approve action? (y/n)"
        self.assertEqual(self.mod.classify_codex_output(output), "READY")

    def test_codex_run_when_processing(self):
        output = "thinking about this"
        self.assertEqual(self.mod.classify_codex_output(output), "RUN")

    def test_claude_ready_at_prompt(self):
        output = "Result\n> "
        self.assertEqual(self.mod.classify_claude_output(output), "READY")

    def test_claude_ready_on_selection_prompt(self):
        output = "\u276f 1. option"
        self.assertEqual(self.mod.classify_claude_output(output), "READY")

    def test_claude_run_when_processing(self):
        output = "\u2736 ... \u2026 (esc to interrupt)"
        self.assertEqual(self.mod.classify_claude_output(output), "RUN")

    def test_window_state_prefers_run(self):
        states = ["READY", "RUN", "READY"]
        self.assertEqual(self.mod.aggregate_window_state(states), "RUN")
```

**Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests/test_annotator_status.py -v`
Expected: FAIL with `AttributeError` (missing `classify_*` helpers).

**Step 3: Write minimal implementation**

```python
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"
CODEX_IDLE_PROMPT_PATTERN = r"(?:\u276f|\u203a|codex>)"
CODEX_IDLE_PROMPT_AT_END_PATTERN = rf"(?:^\s*{CODEX_IDLE_PROMPT_PATTERN}\s*$)\s*\Z"
CODEX_WAITING_PROMPT_PATTERN = r"^(?:Approve|Allow)\b.*\b(?:y/n|yes/no|yes|no)\b"
CODEX_ERROR_PATTERN = r"^(?:Error:|ERROR:|Traceback \(most recent call last\):|panic:)"
CODEX_PROCESSING_PATTERN = r"\b(thinking|working|running|executing|processing|analyzing)\b"

CLAUDE_IDLE_PROMPT_PATTERN = r">(?:\s|\u00a0)"
CLAUDE_IDLE_PROMPT_AT_END_PATTERN = rf"(?:^\s*{CLAUDE_IDLE_PROMPT_PATTERN}\s*$)\s*\Z"
CLAUDE_WAITING_PROMPT_PATTERN = r"\u276f.*\d+\."
CLAUDE_PROCESSING_PATTERN = r"[\u2736\u2722\u273d\u273b\u00b7\u2733].*\u2026.*\(esc to interrupt.*\)"


def classify_codex_output(output: Optional[str]) -> str:
    if not output:
        return "ERR"
    clean = strip_ansi(output)
    tail_output = tail_lines(clean)
    if re.search(CODEX_WAITING_PROMPT_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
        return "READY"
    if re.search(CODEX_IDLE_PROMPT_AT_END_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
        return "READY"
    if re.search(CODEX_ERROR_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
        return "ERR"
    if re.search(CODEX_PROCESSING_PATTERN, tail_output, re.IGNORECASE):
        return "RUN"
    return "RUN"


def classify_claude_output(output: Optional[str]) -> str:
    if not output:
        return "ERR"
    clean = strip_ansi(output)
    tail_output = tail_lines(clean)
    if re.search(CLAUDE_WAITING_PROMPT_PATTERN, tail_output, re.MULTILINE):
        return "READY"
    if re.search(CLAUDE_IDLE_PROMPT_AT_END_PATTERN, tail_output, re.MULTILINE):
        return "READY"
    if re.search(CLAUDE_PROCESSING_PATTERN, tail_output, re.MULTILINE):
        return "RUN"
    return "RUN"


def aggregate_window_state(states: list[str]) -> str:
    if not states:
        return "ERR"
    if "RUN" in states:
        return "RUN"
    if "ERR" in states:
        return "ERR"
    return "READY"
```

**Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests/test_annotator_status.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add bin/codex-annotator tests/test_annotator_status.py
git commit -m "feat: detect ready prompts for codex and claude"
```

### Task 2: Update docs for new config

**Files:**
- Modify: `README.md`

**Step 1: Write the failing test**

No automated test. Manual doc update.

**Step 2: Implement**

Add `CODEX_ANNOTATOR_CAPTURE_LINES` to the Environment Variables list and mention prompt parsing in the Status Annotations section.

**Step 3: Commit**

```bash
git add README.md
git commit -m "docs: describe annotator prompt capture"
```

### Task 3: Verify end-to-end

**Step 1: Run validation**

Run: `./validate.sh`
Expected: PASS

**Step 2: Manual smoke check**

Attach to a Codex or Claude tmux window and confirm READY appears only when the prompt is visible or when approval/selection prompts are shown.

**Step 3: Commit**

No commit if no changes.
