import argparse
import importlib.machinery
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
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
        if cmd[:2] == ["tmux", "set-window-option"]:
            return ""
        if cmd[:2] == ["tmux", "display-message"]:
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

        rename_window_calls = [cmd for cmd in stub.commands if cmd[:2] == ["tmux", "rename-window"]]
        self.assertEqual(rename_window_calls, [], "Titles are not rewritten by default")

        status_updates = [
            cmd
            for cmd in stub.commands
            if cmd[:2] == ["tmux", "set-window-option"]
            and len(cmd) >= 6
            and cmd[-2] == annotator.TMUX_STATE_OPTION
        ]
        self.assertEqual(
            status_updates,
            [
                ["tmux", "set-window-option", "-q", "-t", "@1", "@codex_state", "RUN"],
                ["tmux", "set-window-option", "-q", "-t", "@2", "@codex_state", "READY"],
            ],
        )

    def test_legacy_title_updates_can_be_enabled(self):
        annotator = load_annotator_module()
        stub = RunTmuxStub()
        annotator.run_tmux = stub  # type: ignore[assignment]

        annotator.annotate_once(
            re.compile(r"^codex"),
            re.compile(r"python"),
            update_titles=True,
            notify_on_ready=False,
            verbose=False,
        )

        rename_window_calls = [cmd for cmd in stub.commands if cmd[:2] == ["tmux", "rename-window"]]
        self.assertEqual(
            rename_window_calls,
            [
                ["tmux", "rename-window", "-t", "@1", "*RUN* alpha"],
                ["tmux", "rename-window", "-t", "@2", "*READY* beta"],
            ],
        )

    def test_notifies_when_window_transitions_to_ready(self):
        annotator = load_annotator_module()
        stub = RunTmuxStub()
        annotator.run_tmux = stub  # type: ignore[assignment]
        state_cache = {"@2": "RUN"}

        annotator.annotate_once(
            re.compile(r"^codex"),
            re.compile(r"python"),
            state_cache=state_cache,
            update_titles=False,
            notify_on_ready=True,
            ready_message="{name} is ready",
            now=1234,
            verbose=False,
        )

        self.assertEqual(state_cache["@2"], "READY")
        self.assertIn(
            ["tmux", "display-message", "-t", "@2", "beta is ready"],
            stub.commands,
        )
        self.assertIn(
            [
                "tmux",
                "set-window-option",
                "-q",
                "-t",
                "@2",
                "@codex_last_ready",
                "1234",
            ],
            stub.commands,
        )

    def test_annotates_named_farm_from_session_registry(self):
        annotator = load_annotator_module()

        class RegistryRunTmuxStub:
            def __init__(self):
                self.commands = []

            def __call__(self, cmd, *, verbose=False):  # noqa: ARG002
                self.commands.append(cmd)
                if cmd == ["tmux", "list-sessions", "-F", "#{session_id}\t#{session_name}"]:
                    return "$1\tcodexfarm\n$2\twork\n"
                if cmd[:2] == ["tmux", "list-windows"] and cmd[3] == "$1":
                    return ""
                if cmd[:2] == ["tmux", "list-windows"] and cmd[3] == "$2":
                    return "@10\talpha\n"
                if cmd[:2] == ["tmux", "list-panes"] and cmd[3] == "@10":
                    return "%10\tbash\t0\n"
                if cmd[:2] == ["tmux", "rename-window"]:
                    return ""
                if cmd[:2] == ["tmux", "set-window-option"]:
                    return ""
                if cmd[:2] == ["tmux", "display-message"]:
                    return ""
                raise AssertionError(f"Unexpected tmux command: {cmd}")

        stub = RegistryRunTmuxStub()
        annotator.run_tmux = stub  # type: ignore[assignment]

        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "managed_sessions"
            registry.write_text("work\n", encoding="utf-8")
            annotator.SESSION_REGISTRY = str(registry)

            annotator.annotate_once(
                re.compile(r"^codex"),
                re.compile(r"python"),
                update_titles=True,
                notify_on_ready=False,
                verbose=False,
            )

        rename_window_calls = [cmd for cmd in stub.commands if cmd[:2] == ["tmux", "rename-window"]]
        self.assertEqual(
            rename_window_calls,
            [["tmux", "rename-window", "-t", "@10", "*READY* alpha"]],
        )

    def test_preserves_memory_flag_when_updating_status(self):
        annotator = load_annotator_module()
        item = annotator.WindowInfo(
            sid="$1",
            wid="@1",
            current_name="*200+MB** *READY* alpha",
            base_name=annotator.strip_status_prefix("*200+MB** *READY* alpha"),
            state="RUN",
        )

        self.assertEqual(item.base_name, "alpha")
        self.assertEqual(annotator.desired_name(item), "*200+MB** *RUN* alpha")

    def test_normalizes_memory_flag_after_status_prefix(self):
        annotator = load_annotator_module()
        item = annotator.WindowInfo(
            sid="$1",
            wid="@1",
            current_name="*RUN* *512+MB** alpha",
            base_name=annotator.strip_status_prefix("*RUN* *512+MB** alpha"),
            state="READY",
        )

        self.assertEqual(item.base_name, "alpha")
        self.assertEqual(annotator.desired_name(item), "*512+MB** *READY* alpha")

    def test_invalid_ready_template_falls_back_safely(self):
        annotator = load_annotator_module()
        stub = RunTmuxStub()
        annotator.run_tmux = stub  # type: ignore[assignment]
        state_cache = {"@2": "RUN"}

        annotator.annotate_once(
            re.compile(r"^codex"),
            re.compile(r"python"),
            state_cache=state_cache,
            update_titles=False,
            notify_on_ready=True,
            ready_message="{missing",
            now=1234,
            verbose=False,
        )

        self.assertIn(["tmux", "display-message", "-t", "@2", "READY: beta"], stub.commands)

    def test_annotation_prunes_stale_cached_windows(self):
        annotator = load_annotator_module()
        stub = RunTmuxStub()
        annotator.run_tmux = stub  # type: ignore[assignment]
        state_cache = {"@1": "RUN", "@gone": "RUN"}

        annotator.annotate_once(
            re.compile(r"^codex"),
            re.compile(r"python"),
            state_cache=state_cache,
            update_titles=False,
            notify_on_ready=False,
            verbose=False,
        )

        self.assertNotIn("@gone", state_cache)

    def test_interval_validation_rejects_non_finite_values(self):
        annotator = load_annotator_module()

        for value in ["0", "-1", "nan", "inf", "-inf"]:
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    annotator.parse_positive_interval(value)

    def test_cli_handles_lockfile_without_directory_and_invalid_regex(self):
        annotator_path = Path(__file__).resolve().parent.parent / "bin" / "codex-annotator.py"
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["CODEX_ANNOTATOR_LOCKFILE"] = "annotator.lock"
            result = subprocess.run(
                [
                    sys.executable,
                    str(annotator_path),
                    "--once",
                    "--session-regex",
                    "[",
                ],
                cwd=tmp,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Invalid regular expression", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
