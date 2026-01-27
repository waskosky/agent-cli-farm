import importlib.machinery
import importlib.util
import sys
import re
import unittest
from pathlib import Path


def load_annotator_module():
    """Load the codex-annotator script as a module for testing."""
    annotator_path = Path(__file__).resolve().parent.parent / "bin" / "codex-annotator.py"
    loader = importlib.machinery.SourceFileLoader(
        "codex_annotator_test_module", str(annotator_path)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("Unable to load codex-annotator module")
    module = importlib.util.module_from_spec(spec)
    if spec.loader is None:
        raise RuntimeError("Unable to load codex-annotator module")
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[arg-type]
    return module


class RunTmuxStub:
    """Stub tmux interactions to observe commands and return canned responses."""

    def __init__(self):
        self.commands = []

    def __call__(self, cmd, *, verbose=False):  # noqa: ARG002 - verbose unused
        self.commands.append(cmd)
        if cmd == ["tmux", "list-sessions", "-F", "#{session_id}\t#{session_name}"]:
            # Simulate a session name that was already prefixed; annotator should ignore it.
            return "$1\t*RUN* codexfarm\n"
        if cmd[:2] == ["tmux", "list-windows"] and "-F" in cmd:
            return "@1\talpha\n@2\tbeta\n"
        if cmd[:2] == ["tmux", "list-panes"] and "-F" in cmd:
            target = cmd[3] if len(cmd) > 3 else ""
            if target == "@1":
                # One running pane
                return "%1\tpython3\t0\n%2\tbash\t1\n"
            if target == "@2":
                # Live but not running pane
                return "%3\tbash\t0\n"
        if cmd[:2] == ["tmux", "rename-window"]:
            return ""
        raise AssertionError(f"Unexpected tmux command: {cmd}")


class AnnotatorWindowTests(unittest.TestCase):
    def test_annotates_windows_and_leaves_sessions(self):
        annotator = load_annotator_module()
        stub = RunTmuxStub()
        # Patch run_tmux to our stub
        annotator.run_tmux = stub  # type: ignore[assignment]

        session_pattern = re.compile(r"^codex")
        running_pattern = re.compile(r"python")

        annotator.annotate_once(session_pattern, running_pattern, verbose=False)

        rename_session_calls = [
            cmd for cmd in stub.commands if cmd[:2] == ["tmux", "rename-session"]
        ]
        self.assertEqual(rename_session_calls, [], "Sessions should not be renamed")

        rename_window_calls = [
            cmd for cmd in stub.commands if cmd[:2] == ["tmux", "rename-window"]
        ]
        self.assertEqual(
            rename_window_calls,
            [
                ["tmux", "rename-window", "-t", "@1", "*RUN* alpha"],
                ["tmux", "rename-window", "-t", "@2", "*READY* beta"],
            ],
        )


if __name__ == "__main__":
    unittest.main()
