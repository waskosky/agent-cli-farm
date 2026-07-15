from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_looper import status_state


class StatusStateTest(unittest.TestCase):
    def test_process_is_running_treats_linux_zombies_as_stopped(self) -> None:
        with mock.patch.object(status_state, "_linux_proc_state", return_value="Z"):
            self.assertFalse(status_state.process_is_running(12345))

    def test_parse_linux_proc_state_handles_parentheses_in_command_name(self) -> None:
        self.assertEqual(
            status_state._parse_linux_proc_state("123 (cmd with ) paren) S 1 2 3"),
            "S",
        )

    def test_parse_linux_proc_start_time_handles_parentheses_in_command_name(self) -> None:
        fields = ["S", *[str(value) for value in range(1, 19)], "424242"]

        self.assertEqual(
            status_state._parse_linux_proc_start_time(
                "123 (cmd with ) paren) " + " ".join(fields)
            ),
            "424242",
        )

    def test_active_state_stale_reason_marks_dead_supervisor(self) -> None:
        with mock.patch.object(status_state, "process_is_running", return_value=False):
            reason = status_state.active_state_stale_reason({"status": "running", "pid": 12345})

        self.assertEqual(reason, "supervisor process 12345 is no longer running")

    def test_active_state_stale_reason_keeps_live_supervisor_active(self) -> None:
        with mock.patch.object(status_state, "process_is_running", return_value=True):
            reason = status_state.active_state_stale_reason({"status": "retrying", "pid": 12345})

        self.assertIsNone(reason)

    def test_active_state_stale_reason_rejects_reused_supervisor_pid(self) -> None:
        with (
            mock.patch.object(status_state, "process_is_running", return_value=True),
            mock.patch.object(status_state, "process_identity", return_value="current-token"),
        ):
            reason = status_state.active_state_stale_reason(
                {
                    "status": "running",
                    "pid": 12345,
                    "pid_identity": "original-token",
                }
            )

        self.assertEqual(reason, "supervisor process 12345 identity changed")

    def test_repair_stale_state_file_records_stopped_state_and_event(self) -> None:
        with mock.patch.object(status_state, "process_is_running", return_value=False):
            tempdir = tempfile.TemporaryDirectory(prefix="status-state-test-")
            self.addCleanup(tempdir.cleanup)
            root = Path(tempdir.name)
            run_dir = root / "run"
            run_dir.mkdir()
            state_path = run_dir / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "running",
                        "pid": 12345,
                        "label": "repair-me",
                        "updated_at": "2026-07-02T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )

            repaired, changed, reason = status_state.repair_stale_state_file(
                state_path,
                stamp="2026-07-02T00:00:10Z",
            )

        self.assertTrue(changed)
        self.assertEqual(reason, "supervisor process 12345 is no longer running")
        self.assertEqual(repaired["status"], "stopped")
        self.assertEqual(repaired["last_event"], "run_stopped")
        self.assertIn("external termination", repaired["stop_reason"])

        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["completed_at"], "2026-07-02T00:00:10Z")

        events = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(events), 1)
        event = json.loads(events[0])
        self.assertEqual(event["event"], "run_stopped")
        self.assertEqual(event["status"], "stopped")

    def test_iter_looper_state_views_marks_stale_without_writing(self) -> None:
        with mock.patch.object(status_state, "process_is_running", return_value=False):
            tempdir = tempfile.TemporaryDirectory(prefix="status-state-test-")
            self.addCleanup(tempdir.cleanup)
            root = Path(tempdir.name)
            run_dir = root / "runs" / "run"
            run_dir.mkdir(parents=True)
            state_path = run_dir / "state.json"
            state_path.write_text(
                json.dumps(
                    {"status": "running", "pid": 12345, "updated_at": "2026-07-02T00:00:00Z"}
                ),
                encoding="utf-8",
            )

            views = list(status_state.iter_looper_state_views(root / "runs"))

        self.assertEqual(len(views), 1)
        self.assertTrue(views[0].stale)
        self.assertEqual(views[0].state["status"], "running")
        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["status"], "running")


if __name__ == "__main__":
    unittest.main()
