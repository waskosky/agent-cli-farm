import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codex_looper.resource_config import ResourceSettings
from codex_looper.resource_jobs import JobStore, _wait
from codex_looper.storage import GIB, StorageSettings, _snapshot, admission_reason, main, pressure, probe


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.store = JobStore(self.state / "resources/jobs")
        self.settings = StorageSettings(enabled=True)
        self.addCleanup(self.temp.cleanup)

    def test_virtual_free_space_cannot_override_host(self):
        with (
            patch("codex_looper.storage._is_wsl", return_value=True),
            patch(
                "codex_looper.storage._windows_probe",
                return_value={"free": 9 * GIB, "total": 500 * GIB, "volume": "windows:D:"},
            ),
        ):
            sample = probe(self.root, self.settings)
        self.assertLessEqual(sample["free"], 9 * GIB)
        self.assertIn("headroom", pressure(sample, self.settings, running=True))


    def test_filesystem_capacity_limits_admission_and_running_checkpoints(self):
        sample = {"available": True, "free": 120 * GIB, "filesystem_free": 20 * GIB}
        self.assertIn("20.0 GiB", pressure(sample, self.settings))
        self.assertIsNone(pressure(sample, self.settings, running=True))
        sample["filesystem_free"] = 9 * GIB
        self.assertIn("9.0 GiB", pressure(sample, self.settings, running=True))

    def test_peak_reservation_must_fit_inside_filesystem(self):
        sample = {"available": True, "free": 120 * GIB, "filesystem_free": 30 * GIB}
        self.assertIsNone(pressure(sample, self.settings, additional=5 * GIB))
        self.assertIsNotNone(pressure(sample, self.settings, additional=6 * GIB))

    def test_shared_reservations_cannot_overcommit_capped_filesystem(self):
        with (
            patch("codex_looper.storage.load_settings", return_value=self.settings),
            patch("codex_looper.storage.state_directory", return_value=self.state),
            patch("codex_looper.storage.probe", return_value={
                "available": True, "free": 120 * GIB, "filesystem_free": 60 * GIB,
                "volume": "windows:D:",
            }),
        ):
            first = self.store.create("batch", ["one"], str(self.root), reserve_disk_bytes=20 * GIB)
            second = self.store.create("batch", ["two"], str(self.root), reserve_disk_bytes=20 * GIB)
            self.assertIsNone(self.store.reserve_admission(first["job_id"], ResourceSettings()))
            self.assertIn("headroom", self.store.reserve_admission(second["job_id"], ResourceSettings()))

    def test_guest_pressure_requires_guest_recovery_even_with_host_space(self):
        self.store._prepare()
        record = {"cwd": str(self.root), "reserve_disk_bytes": 0}
        sample = {"available": True, "free": 120 * GIB, "filesystem_free": 20 * GIB,
                  "volume": "windows:D:"}
        with (
            patch("codex_looper.storage.load_settings", return_value=self.settings),
            patch("codex_looper.storage.state_directory", return_value=self.state),
            patch("codex_looper.storage.probe", return_value=sample),
        ):
            self.assertIn("headroom", admission_reason(record, []))
            sample["filesystem_free"] = 30 * GIB
            with patch("codex_looper.storage.time.time", return_value=1000):
                self.assertIn("recovery", admission_reason(record, []))
            sample["filesystem_free"] = 60 * GIB
            with patch("codex_looper.storage.time.time", return_value=1100):
                self.assertIn("recovery", admission_reason(record, []))
            with patch("codex_looper.storage.time.time", return_value=1221):
                self.assertIsNone(admission_reason(record, []))

    def test_status_warns_when_filesystem_is_tighter_than_backing_drive(self):
        output = io.StringIO()
        with (
            patch("codex_looper.storage.load_settings", return_value=self.settings),
            patch("codex_looper.storage.probe", return_value={
                "available": True, "free": 120 * GIB, "filesystem_free": 50 * GIB,
                "volume": "windows:D:",
            }),
            patch("sys.stdout", output),
        ):
            self.assertEqual(main(["status", "--path", str(self.root)]), 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["warning"])
        self.assertIsNone(result["reason"])
        self.assertEqual(result["free"], 120 * GIB)

    def test_stale_missing_and_future_snapshot_fail_closed(self):
        path = self.root / "snapshot.json"
        value = {
            "schema": "agent-farm-storage-v1",
            "sampled_at": 100,
            "volumes": {"D:": {"free": 90 * GIB, "total": 500 * GIB}},
            "distributions": {"Ubuntu": "D:"},
        }
        path.write_text(json.dumps(value))
        self.assertEqual(_snapshot(path, "Ubuntu", None, 110)["volume"], "windows:D:")
        for now in (50, 131):
            with self.assertRaises(ValueError):
                _snapshot(path, "Ubuntu", None, now)
        with self.assertRaises(KeyError):
            _snapshot(path, "Unknown", None, 110)
        link = self.root / "link"
        link.symlink_to(path)
        with self.assertRaises(ValueError):
            _snapshot(link, "Ubuntu", None, 110)

    def test_reservations_share_physical_volume_and_release_finished(self):
        with (
            patch("codex_looper.storage.load_settings", return_value=self.settings),
            patch("codex_looper.storage.state_directory", return_value=self.state),
            patch(
                "codex_looper.storage.probe",
                return_value={
                    "available": True,
                    "free": 60 * GIB,
                    "volume": "windows:D:",
                    "inode_free": 100000,
                },
            ),
        ):
            first = self.store.create("batch", ["one"], str(self.root), reserve_disk_bytes=20 * GIB)
            second = self.store.create(
                "batch", ["two"], str(self.root), reserve_disk_bytes=20 * GIB
            )
            self.assertIsNone(self.store.reserve_admission(first["job_id"], ResourceSettings()))
            self.assertIn(
                "headroom", self.store.reserve_admission(second["job_id"], ResourceSettings())
            )
            self.store.update(first["job_id"], status="finished")
            self.assertIsNone(self.store.reserve_admission(second["job_id"], ResourceSettings()))

    def test_physical_pressure_requires_sustained_recovery(self):
        self.store._prepare()
        record = {"cwd": str(self.root), "reserve_disk_bytes": 0}
        sample = {"available": True, "free": 20 * GIB, "volume": "windows:D:"}
        with (
            patch("codex_looper.storage.load_settings", return_value=self.settings),
            patch("codex_looper.storage.state_directory", return_value=self.state),
            patch("codex_looper.storage.probe", return_value=sample),
        ):
            self.assertIn("headroom", admission_reason(record, []))
            sample["free"] = 60 * GIB
            with patch("codex_looper.storage.time.time", return_value=1000):
                self.assertIn("recovery", admission_reason(record, []))
            with patch("codex_looper.storage.time.time", return_value=1121):
                self.assertIsNone(admission_reason(record, []))

    def test_oversized_waiter_does_not_hold_smaller_safe_work(self):
        self.store._prepare()
        with (
            patch("codex_looper.storage.load_settings", return_value=self.settings),
            patch("codex_looper.storage.state_directory", return_value=self.state),
            patch(
                "codex_looper.storage.probe",
                return_value={"available": True, "free": 60 * GIB, "volume": "windows:D:"},
            ),
        ):
            self.assertIsNotNone(
                admission_reason({"cwd": str(self.root), "reserve_disk_bytes": 100 * GIB}, [])
            )
            self.assertIsNone(
                admission_reason({"cwd": str(self.root), "reserve_disk_bytes": GIB}, [])
            )

    def test_unknown_peer_is_not_reclaimed(self):
        with (
            patch("codex_looper.storage.load_settings", return_value=self.settings),
            patch("codex_looper.storage.state_directory", return_value=self.state),
            patch(
                "codex_looper.storage.probe",
                return_value={
                    "available": True,
                    "free": 60 * GIB,
                    "volume": "windows:D:",
                    "inode_free": 100000,
                },
            ),
        ):
            first = self.store.create("batch", ["one"], str(self.root), reserve_disk_bytes=20 * GIB)
            self.assertIsNone(self.store.reserve_admission(first["job_id"], ResourceSettings()))
            second = self.store.create(
                "batch", ["two"], str(self.root), reserve_disk_bytes=20 * GIB
            )
            with patch(
                "codex_looper.resource_jobs._recorded_process_state",
                side_effect=lambda r, n: "live" if r["job_id"] == second["job_id"] else "unknown",
            ):
                self.assertIn(
                    "headroom", self.store.reserve_admission(second["job_id"], ResourceSettings())
                )

    def test_agent_rejects_disk_remedy_and_reservation(self):
        for kwargs in ({"reserve_disk_bytes": GIB}, {"stop_on_disk_pressure": True}):
            with self.assertRaises(ValueError):
                self.store.create("agent", ["agent"], str(self.root), **kwargs)

    def test_inode_and_unknown_measurement_block_writers(self):
        self.assertIsNotNone(pressure({"available": False}, self.settings))
        self.assertIn(
            "inode",
            pressure({"available": True, "free": 200 * GIB, "inode_free": 5}, self.settings),
        )

    def test_independent_volumes_do_not_share_reservations(self):
        self.store._prepare()
        with (
            patch("codex_looper.storage.load_settings", return_value=self.settings),
            patch("codex_looper.storage.state_directory", return_value=self.state),
            patch(
                "codex_looper.storage.probe",
                return_value={"available": True, "free": 60 * GIB, "volume": "windows:D:"},
            ),
        ):
            self.assertIsNone(
                admission_reason(
                    {"cwd": str(self.root), "reserve_disk_bytes": 20 * GIB},
                    [{"storage_volume": "windows:C:", "reserve_disk_bytes": 500 * GIB}],
                )
            )

    def test_emergency_stop_requires_the_exact_live_batch_identity(self):
        for valid in (False, True):
            store = Mock()
            store.identity.return_value = {"job_id": "owned"}
            store.validate.return_value = valid
            process = Mock()
            process.poll.side_effect = [None, 0]
            process.returncode = 0
            record = {
                "job_id": "owned",
                "role": "batch",
                "storage_path": str(self.root),
                "stop_on_disk_pressure": True,
            }
            store.request_path.return_value = self.root / "missing-restart"
            with (
                patch("codex_looper.storage.checkpoint", return_value="disk emergency"),
                patch("codex_looper.resource_jobs.terminate_owned_batch") as terminate,
            ):
                code, restart = _wait(store, record, process, ResourceSettings(), "warn")
            self.assertFalse(restart)
            self.assertEqual(code, 75 if valid else 0)
            self.assertEqual(terminate.call_count, 1 if valid else 0)

    def test_windows_system_or_swap_volume_can_hold_another_drive(self):
        sample = {"available": True, "free": 100 * GIB, "host_guard_free": 9 * GIB}
        self.assertIn("system/swap", pressure(sample, self.settings, running=True))

    def test_status_warns_when_system_or_swap_is_the_limiting_volume(self):
        from codex_looper.storage import main

        sample = {"available": True, "free": 100 * GIB, "host_guard_free": 20 * GIB}
        output = io.StringIO()
        with (
            patch("codex_looper.storage.load_settings", return_value=self.settings),
            patch("codex_looper.storage.probe", return_value=sample),
            patch("sys.stdout", output),
        ):
            self.assertEqual(main(["status", "--path", str(self.root)]), 0)
        self.assertTrue(json.loads(output.getvalue())["warning"])

    def test_configuration_rejects_bad_order_and_nonfinite(self):
        for values in (
            {"reserve_gib": 5},
            {"emergency_gib": float("nan")},
            {"enabled": 1},
            {"windows_snapshot": "relative"},
        ):
            with self.assertRaises(ValueError):
                StorageSettings(**values)


if __name__ == "__main__":
    unittest.main()
