from __future__ import annotations

import contextlib
import io
import json
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_looper import control
from codex_looper import control_panel
from codex_looper import tmux


class ControlPanelTests(unittest.TestCase):
    def test_control_pane_command_targets_run_pointer_and_supervisor(self) -> None:
        command = tmux.control_pane_command(
            run_dir=Path("/tmp/run"),
            pointer_path=Path("/tmp/run/current-log.path"),
            supervisor_pid=42,
        )

        parts = shlex.split(command)
        self.assertIn("control-pane", parts)
        self.assertIn("--run-dir", parts)
        self.assertIn("/tmp/run", parts)
        self.assertIn("--pointer", parts)
        self.assertIn("/tmp/run/current-log.path", parts)
        self.assertIn("--supervisor-pid", parts)
        self.assertIn("42", parts)

    def test_run_control_pane_action_queues_safe_stop(self) -> None:
        tempdir = tempfile.TemporaryDirectory(prefix="control-pane-test-")
        self.addCleanup(tempdir.cleanup)
        run_dir = Path(tempdir.name)

        result = control_panel.run_control_pane_action("p", run_dir=run_dir)

        self.assertEqual(result.message, "queued stop_after_prompt")
        control_lines = (run_dir / "control.jsonl").read_text(encoding="utf-8").splitlines()
        record = json.loads(control_lines[0])
        self.assertEqual(record["action"], "stop_after_prompt")
        self.assertEqual(record["reason"], "control pane: stop after prompt")

    def test_run_control_pane_action_interrupts_now(self) -> None:
        tempdir = tempfile.TemporaryDirectory(prefix="control-pane-test-")
        self.addCleanup(tempdir.cleanup)
        run_dir = Path(tempdir.name)
        (run_dir / "state.json").write_text(
            json.dumps({"status": "running", "pid": 123, "run_dir": str(run_dir)}),
            encoding="utf-8",
        )

        with mock.patch.object(control_panel, "interrupt_from_state", return_value="interrupted"):
            result = control_panel.run_control_pane_action("i", run_dir=run_dir)

        self.assertEqual(result.message, "interrupted")
        control_lines = (run_dir / "control.jsonl").read_text(encoding="utf-8").splitlines()
        record = json.loads(control_lines[0])
        self.assertEqual(record["action"], "interrupt_now")

    def test_run_control_pane_action_repairs_stale_state(self) -> None:
        tempdir = tempfile.TemporaryDirectory(prefix="control-pane-test-")
        self.addCleanup(tempdir.cleanup)
        run_dir = Path(tempdir.name)

        with mock.patch.object(
            control_panel,
            "repair_stale_state_file",
            return_value=({}, True, "supervisor process 123 is no longer running"),
        ):
            result = control_panel.run_control_pane_action("r", run_dir=run_dir)

        self.assertIn("repaired stale looper state", result.message)

    def test_emit_new_log_lines_renders_transcript_and_returns_offset(self) -> None:
        tempdir = tempfile.TemporaryDirectory(prefix="control-pane-test-")
        self.addCleanup(tempdir.cleanup)
        log_path = Path(tempdir.name) / "loop.log"
        log_path.write_text("[20260704T000000Z] stdout: hello\n", encoding="utf-8")
        output = io.StringIO()

        offset = control_panel.emit_new_log_lines(
            log_path=log_path,
            offset=0,
            output_stream=output,
        )
        offset_after_second_read = control_panel.emit_new_log_lines(
            log_path=log_path,
            offset=offset,
            output_stream=output,
        )

        self.assertEqual(output.getvalue(), "hello\n")
        self.assertEqual(offset_after_second_read, offset)

    def test_format_state_summary_includes_focus_summary(self) -> None:
        rendered = control_panel.format_state_summary(
            {"label": "run", "status": "running"},
            log_path=None,
            focus_summary="Improving looper status visibility.",
        )

        self.assertIn("focus: Improving looper status visibility.", rendered)

    def test_render_header_reads_latest_focus_summary(self) -> None:
        tempdir = tempfile.TemporaryDirectory(prefix="control-pane-focus-test-")
        self.addCleanup(tempdir.cleanup)
        run_dir = Path(tempdir.name)
        pointer = run_dir / "current-log.path"
        pointer.write_text("", encoding="utf-8")
        (run_dir / "state.json").write_text(
            json.dumps({"label": "run", "status": "running"}),
            encoding="utf-8",
        )
        control.append_focus_update(
            run_dir,
            summary="Checking whether screenshots are usable.",
            command_id="focus-test",
        )
        output = io.StringIO()

        control_panel.render_header(run_dir, pointer, output)

        self.assertIn("focus: Checking whether screenshots are usable.", output.getvalue())

    def test_control_pane_main_accepts_quit_action(self) -> None:
        tempdir = tempfile.TemporaryDirectory(prefix="control-pane-test-")
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        run_dir = root / "run"
        run_dir.mkdir()
        pointer = run_dir / "current-log.path"
        pointer.write_text("", encoding="utf-8")
        output = io.StringIO()

        with (
            contextlib.redirect_stderr(io.StringIO()),
            mock.patch.object(control_panel, "_stdin_ready", return_value=True),
        ):
            status = control_panel.control_pane_main(
                [
                    "--run-dir",
                    str(run_dir),
                    "--pointer",
                    str(pointer),
                    "--supervisor-pid",
                    "0",
                    "--refresh-seconds",
                    "0.05",
                ],
                input_stream=io.StringIO("q\n"),
                output_stream=output,
            )

        self.assertEqual(status, 0)
        self.assertIn("leaving looper control pane", output.getvalue())


if __name__ == "__main__":
    unittest.main()
