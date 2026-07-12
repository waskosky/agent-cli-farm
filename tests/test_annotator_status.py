import importlib.util
import sys
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


def load_annotator_module():
    annotator_path = Path(__file__).resolve().parents[1] / "bin" / "codex-annotator.py"
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

    def test_codex_ready_without_processing_marker(self):
        output = "still busy...\nno prompt yet"
        self.assertEqual(self.mod.classify_codex_output(output), "READY")

    def test_claude_ready_at_prompt(self):
        output = "Result\n> "
        self.assertEqual(self.mod.classify_claude_output(output), "READY")

    def test_claude_ready_on_selection_prompt(self):
        output = "\u276f 1. option"
        self.assertEqual(self.mod.classify_claude_output(output), "READY")

    def test_claude_run_when_processing(self):
        output = "\u2736 ... \u2026 (esc to interrupt)"
        self.assertEqual(self.mod.classify_claude_output(output), "RUN")

    def test_claude_run_when_modern_status_line_is_active(self):
        output = "\u273b Vibing\u2026 (2m 44s \u00b7 \u2193 12.0k tokens)"
        self.assertEqual(self.mod.classify_claude_output(output), "RUN")

    def test_claude_run_when_unknown_modern_status_verb_is_active(self):
        output = "\u273d Topsy-turvying\u2026 (25s \u00b7 \u2193 1.4k tokens)"
        self.assertEqual(self.mod.classify_claude_output(output), "RUN")

    def test_claude_run_when_spinner_status_verb_changes(self):
        output = "\u2736 Flibbertigibbeting\u2026"
        self.assertEqual(self.mod.classify_claude_output(output), "RUN")

    def test_claude_idle_prompt_overrides_stale_processing_spinner(self):
        output = "\u2736 Flibbertigibbeting\u2026\nCompleted the task.\n> "
        self.assertEqual(self.mod.classify_claude_output(output), "READY")

    def test_claude_ready_without_processing_marker(self):
        output = "Output line without prompt"
        self.assertEqual(self.mod.classify_claude_output(output), "READY")

    def test_window_state_prefers_run(self):
        states = ["READY", "RUN", "READY"]
        self.assertEqual(self.mod.aggregate_window_state(states), "RUN")

    def test_looper_start_command_is_running(self):
        pane = self.mod.PaneInfo(
            pid="%1",
            current_command="python3",
            start_command="/home/me/bin/codex-looper.py --agent codex",
            dead=False,
        )

        self.assertEqual(
            self.mod.classify_pane(pane, self.mod.re.compile(r"node"), verbose=False),
            "RUN",
        )

    def test_node_launched_codex_uses_prompt_classification(self):
        pane = self.mod.PaneInfo(
            pid="%1",
            current_command="node",
            start_command="/usr/local/bin/codex",
            dead=False,
        )
        self.mod.capture_pane_output = lambda pane_id, *, verbose: "work complete\n\u276f\n"

        self.assertEqual(
            self.mod.classify_pane(pane, self.mod.re.compile(r"node"), verbose=False),
            "READY",
        )

    def test_node_launched_claude_uses_prompt_classification(self):
        pane = self.mod.PaneInfo(
            pid="%1",
            current_command="node",
            start_command="/usr/local/bin/claude",
            dead=False,
        )
        self.mod.capture_pane_output = lambda pane_id, *, verbose: "result\n> "

        self.assertEqual(
            self.mod.classify_pane(pane, self.mod.re.compile(r"node"), verbose=False),
            "READY",
        )


if __name__ == "__main__":
    unittest.main()
