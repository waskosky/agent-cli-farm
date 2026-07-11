from __future__ import annotations

import contextlib
import io
import json
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_looper import cli, control


class ControlStopTargetTests(unittest.TestCase):
    def test_stop_targets_include_child_group_hybrid_group_and_supervisor(self) -> None:
        def fake_getpgid(pid: int) -> int:
            if pid == 333:
                return 444
            raise ProcessLookupError

        with (
            mock.patch.object(control.os, "name", "posix"),
            mock.patch.object(control.os, "getpgid", side_effect=fake_getpgid),
            mock.patch.object(control, "descendant_pids", return_value=[]),
        ):
            targets = control.stop_targets_from_state(
                {
                    "pid": 111,
                    "child_pid": 222,
                    "child_pgid": 223,
                    "hybrid_pane_pid": 333,
                }
            )

        self.assertEqual(
            [(target.kind, target.identifier, target.label) for target in targets],
            [
                ("process_group", 223, "child process group"),
                ("process_group", 444, "hybrid pane process group"),
                ("process", 111, "supervisor process"),
            ],
        )

    def test_stop_targets_include_hybrid_descendant_groups(self) -> None:
        def fake_getpgid(pid: int) -> int:
            if pid == 333:
                return 444
            if pid == 555:
                return 556
            raise ProcessLookupError

        with (
            mock.patch.object(control.os, "name", "posix"),
            mock.patch.object(control.os, "getpgid", side_effect=fake_getpgid),
            mock.patch.object(control, "descendant_pids", return_value=[555]),
        ):
            targets = control.stop_targets_from_state(
                {"pid": 111, "child_pgid": 223, "hybrid_pane_pid": 333}
            )

        self.assertEqual(
            [(target.kind, target.identifier, target.label) for target in targets],
            [
                ("process_group", 223, "child process group"),
                ("process_group", 556, "hybrid descendant process group"),
                ("process_group", 444, "hybrid pane process group"),
                ("process", 111, "supervisor process"),
            ],
        )

    def test_interrupt_signals_all_resolved_targets(self) -> None:
        calls: list[tuple[str, int, signal.Signals]] = []

        def fake_getpgid(pid: int) -> int:
            if pid == 300:
                return 301
            raise ProcessLookupError

        def fake_killpg(pgid: int, signum: signal.Signals) -> None:
            calls.append(("pg", pgid, signum))

        def fake_kill(pid: int, signum: signal.Signals) -> None:
            calls.append(("pid", pid, signum))

        with (
            mock.patch.object(control.os, "name", "posix"),
            mock.patch.object(control.os, "getpgid", side_effect=fake_getpgid),
            mock.patch.object(control, "descendant_pids", return_value=[]),
            mock.patch.object(control.os, "killpg", side_effect=fake_killpg),
            mock.patch.object(control.os, "kill", side_effect=fake_kill),
        ):
            detail = control.interrupt_from_state(
                {"pid": 100, "child_pgid": 200, "hybrid_pane_pid": 300}
            )

        self.assertEqual(
            calls,
            [
                ("pg", 200, signal.SIGINT),
                ("pg", 301, signal.SIGINT),
                ("pid", 100, signal.SIGINT),
            ],
        )
        self.assertIsNotNone(detail)
        self.assertIn("child process group 200", str(detail))
        self.assertIn("hybrid pane process group 301", str(detail))
        self.assertIn("supervisor process 100", str(detail))

    def test_force_stop_escalates_only_while_targets_are_still_running(self) -> None:
        signals: list[tuple[int, signal.Signals]] = []

        def fake_kill(pid: int, signum: signal.Signals) -> None:
            signals.append((pid, signum))

        running_checks = iter([True, False])

        with mock.patch.object(control.os, "kill", side_effect=fake_kill):
            results = control.force_stop_from_state(
                {"pid": 100},
                grace_seconds=0,
                sleep_fn=lambda _: None,
                target_is_running_fn=lambda _: next(running_checks),
            )

        self.assertEqual(signals, [(100, signal.SIGINT), (100, signal.SIGTERM)])
        self.assertEqual([result.stage for result in results], ["interrupt", "terminate"])


class ControlFocusTests(unittest.TestCase):
    def test_append_focus_update_compacts_summary_and_reads_latest(self) -> None:
        tempdir = tempfile.TemporaryDirectory(prefix="control-focus-test-")
        self.addCleanup(tempdir.cleanup)
        run_dir = Path(tempdir.name)

        first = control.append_focus_update(
            run_dir,
            summary="  Auditing\n\nlooper prompt status visibility.  ",
            actor=" agent ",
            source=" prompt ",
            command_id="focus-1",
        )
        second = control.append_focus_update(
            run_dir,
            summary="Preparing focused tests for the pane display.",
            command_id="focus-2",
        )

        self.assertEqual(first["summary"], "Auditing looper prompt status visibility.")
        self.assertEqual(first["actor"], "agent")
        self.assertEqual(first["source"], "prompt")
        self.assertEqual(second["id"], "focus-2")
        self.assertEqual(control.latest_focus_update(run_dir)["id"], "focus-2")
        records = control.read_focus_updates(run_dir, limit=2)
        self.assertEqual([record["id"] for record in records], ["focus-1", "focus-2"])

    def test_append_focus_update_rejects_blank_summary(self) -> None:
        tempdir = tempfile.TemporaryDirectory(prefix="control-focus-test-")
        self.addCleanup(tempdir.cleanup)

        with self.assertRaises(control.ControlError):
            control.append_focus_update(Path(tempdir.name), summary=" \n\t ")


class ControlCliTests(unittest.TestCase):
    def test_force_stop_queues_interrupt_now_and_repairs_state(self) -> None:
        tempdir = tempfile.TemporaryDirectory(prefix="control-cli-test-")
        self.addCleanup(tempdir.cleanup)
        run_dir = Path(tempdir.name) / "run"
        run_dir.mkdir()
        (run_dir / "state.json").write_text(
            json.dumps({"status": "running", "pid": 100, "run_dir": str(run_dir)}),
            encoding="utf-8",
        )

        with (
            mock.patch.object(cli, "force_stop_from_state", return_value=[]) as force_stop,
            mock.patch.object(
                cli,
                "repair_stale_state_file",
                return_value=({}, False, None),
            ) as repair,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            status = cli.control_main(
                ["stop", "--run-dir", str(run_dir), "--force", "--grace-seconds", "0"]
            )

        self.assertEqual(status, 0)
        control_lines = (run_dir / "control.jsonl").read_text(encoding="utf-8").splitlines()
        record = json.loads(control_lines[0])
        self.assertEqual(record["action"], "interrupt_now")
        force_stop.assert_called_once()
        repair.assert_called_once_with(run_dir / "state.json")

    def test_force_stop_rejects_safe_boundary_flags(self) -> None:
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.control_main(["stop", "run", "--after-loop", "--force"])

    def test_focus_command_records_summary(self) -> None:
        tempdir = tempfile.TemporaryDirectory(prefix="control-cli-focus-test-")
        self.addCleanup(tempdir.cleanup)
        run_dir = Path(tempdir.name) / "run"
        run_dir.mkdir()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            status = cli.control_main(
                [
                    "focus",
                    "--run-dir",
                    str(run_dir),
                    "--summary",
                    "  Verifying\nfocus display.  ",
                    "--actor",
                    "agent",
                    "--source",
                    "prompt",
                ]
            )

        self.assertEqual(status, 0)
        self.assertIn("focus updated", output.getvalue())
        record = json.loads((run_dir / "focus.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(record["summary"], "Verifying focus display.")
        self.assertEqual(record["actor"], "agent")
        self.assertEqual(record["source"], "prompt")


if __name__ == "__main__":
    unittest.main()
