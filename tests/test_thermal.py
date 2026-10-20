import json
import math
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codex_looper import thermal
from codex_looper.resource_config import write_private_json


class ThermalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.env = patch.dict(
            os.environ,
            {
                "HOME": str(self.root),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_STATE_HOME": str(self.root / "state"),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.sensor = self.root / "temperature.json"
        self.settings = thermal.ThermalSettings(
            enabled=True, enforce=True, sensor_file=str(self.sensor)
        )
        self.now = 1000
        self.quotas = {}
        self.commands = []
        kernel = patch("codex_looper.resource_jobs.kernel_limits_effective", return_value=True)
        kernel.start()
        self.addCleanup(kernel.stop)

    def sample(self, value=95, stamp=None, **fields):
        self.sensor.write_text(
            json.dumps(
                {
                    "source": "LibreHardwareMonitor",
                    "checked_at": self.now if stamp is None else stamp,
                    "cpu_celsius": value,
                    **fields,
                }
            )
        )

    def manager(self, args):
        self.commands.append(args)
        if args[0] == "show":
            value = self.quotas.get(args[1], math.inf)
            return subprocess.CompletedProcess(
                args, 0, "infinity" if math.isinf(value) else str(value), ""
            )
        prop = args[-1].split("=", 1)[1]
        self.quotas[args[2]] = float(prop.rstrip("%")) * 10000 if prop else math.inf
        return subprocess.CompletedProcess(args, 0, "", "")

    def store(self, roles=("batch",)):
        jobs = [
            {"job_id": str(n), "role": role, "status": "running", "scope": f"scope-{n}"}
            for n, role in enumerate(roles)
        ]
        store = Mock()
        store.list_jobs.return_value = jobs
        store.identity.side_effect = lambda jid: {"scope": f"scope-{jid}", "uid": os.getuid()}
        store.validate.return_value = True
        return store

    def heartbeat(self, **updates):
        value = {
            "schema_version": 1,
            "guard_version": thermal.GUARD_VERSION,
            "implementation": thermal.IMPLEMENTATION,
            "checked_at": self.now,
            "monotonic_at": self.now,
            "identity": thermal.guard_identity(),
            "settings_fingerprint": thermal.settings_fingerprint(self.settings),
            "configured_enforcement": True,
            "coverage": {"eligible": 0, "verified": 0, "capped": 0, "complete": True},
            "issues": [],
            "cooling_jobs": 0,
        }
        value.update(updates)
        write_private_json(thermal.state_directory() / "thermal-status.json", value)

    def test_fresh_sensor_does_not_hide_missing_dead_stale_or_old_guard(self):
        self.sample(70)
        write_private_json(thermal.settings_path(), thermal.asdict(self.settings))
        self.assertFalse(
            thermal.protection_status(self.settings, self.now)["effective_enforcement"]
        )
        for updates in (
            {"checked_at": self.now - 31},
            {"monotonic_at": self.now + 1},
            {"monotonic_at": self.now - 31},
            {"identity": {"pid": -1}},
            {"implementation": "old"},
            {"schema_version": 2},
            {"settings_fingerprint": "old"},
            {"configured_enforcement": False},
            {"issues": ["batch CPU budget could not be applied"]},
            {"issues": "malformed"},
            {"identity": None},
        ):
            with self.subTest(updates=updates):
                self.heartbeat(**updates)
                status = thermal.protection_status(self.settings, self.now)
                self.assertEqual(status["health"], "degraded")
                self.assertFalse(status["guard_ready"])
                self.assertFalse(status["effective_enforcement"])
        self.heartbeat()
        self.assertTrue(thermal.protection_status(self.settings, self.now)["effective_enforcement"])

    def test_admission_requires_fresh_guard_without_bypassing_agents(self):
        self.sample(70)
        write_private_json(thermal.settings_path(), thermal.asdict(self.settings))
        with (
            patch.object(thermal.time, "time", return_value=self.now),
            patch.object(thermal.time, "monotonic", return_value=self.now),
        ):
            self.assertIn("guard", thermal.admission_reason())
            self.heartbeat()
            self.assertIsNone(thermal.admission_reason())

    def test_guard_status_tracks_effective_protection_and_configuration(self):
        self.sample(70)
        with patch.object(thermal, "manager", side_effect=self.manager):
            status = thermal.Controller(self.root).tick(self.settings, self.store(()), self.now)
        self.assertTrue(status["effective_enforcement"])
        self.assertEqual(status["guard_version"], thermal.GUARD_VERSION)
        self.assertEqual(
            status["settings_fingerprint"], thermal.settings_fingerprint(self.settings)
        )
        self.sensor.unlink()
        status = thermal.Controller(self.root).tick(self.settings, self.store(()), self.now)
        self.assertFalse(status["enforcing"])
        self.assertFalse(status["effective_enforcement"])

    def test_inventory_is_not_truncated_by_first_64_finished_or_agent_jobs(self):
        self.sample()
        store = self.store(("agent",) * 70 + ("batch",))
        with patch.object(thermal, "manager", side_effect=self.manager):
            status = thermal.Controller(self.root).tick(self.settings, store, self.now)
        self.assertEqual(self.quotas, {"scope-70": 2000000})
        self.assertEqual(status["coverage"]["eligible"], 1)
        self.assertTrue(status["coverage"]["complete"])

    def test_sensor_loss_budget_requires_explicit_choice_and_does_not_cap_agents(self):
        from dataclasses import replace

        controller = thermal.Controller(self.root)
        store = self.store(("agent", "batch"))
        settings = replace(self.settings, sensor_loss_policy="budget", sensor_loss_grace_seconds=10)
        with patch.object(thermal, "manager", side_effect=self.manager):
            controller.tick(settings, store, self.now)
            self.assertFalse(self.quotas)
            status = controller.tick(settings, store, self.now + 10)
        self.assertEqual(self.quotas, {"scope-1": 2000000})
        self.assertTrue(status["sensor_loss_budget_active"])
        self.assertFalse(status["effective_enforcement"])

    def test_corrupt_rollback_journal_fails_before_manager_mutations(self):
        write_private_json(self.root / "thermal-remedies.json", {"bad": {"applied_us": True}})
        with patch.object(thermal, "manager") as manager, self.assertRaises(ValueError):
            thermal.Controller(self.root)
        manager.assert_not_called()

    def test_running_unscoped_batch_is_reported_before_critical_heat(self):
        self.sample(70)
        store = self.store()
        store.identity.side_effect = lambda _: {"scope": None, "uid": os.getuid()}
        with patch.object(thermal, "manager", side_effect=self.manager):
            status = thermal.Controller(self.root).tick(self.settings, store, self.now)
        self.assertIn("batch identity unavailable", status["issues"])
        self.assertFalse(status["effective_enforcement"])
        self.assertFalse(status["coverage"]["complete"])
        self.assertFalse(self.quotas)

    def test_watchdog_notifies_only_the_supplied_private_socket(self):
        path = self.root / "notify.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
            server.bind(str(path))
            server.settimeout(1)
            with patch.dict(os.environ, {"NOTIFY_SOCKET": str(path)}):
                thermal.notify_watchdog()
            self.assertIn(b"WATCHDOG=1", server.recv(100))

    def test_cooling_checks_read_cached_data_and_preserve_operator_changes(self):
        thermal.prepare_private_directory(thermal.state_directory())
        write_private_json(
            thermal.state_directory() / "thermal-setup.json", {"cooling_approved": True}
        )
        path = self.sensor.with_name("cooling-status.json")
        self.sample(70)
        self.heartbeat()
        for active, expected in ((thermal.COOL_GUID, "healthy"), ("operator-plan", "degraded")):
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "source": "CodexFarmWindows",
                        "checked_at": self.now,
                        "state": "checked",
                        "active_guid": active,
                        "verified": True,
                    }
                )
            )
            with patch.object(
                thermal.subprocess, "run", side_effect=AssertionError("local reads only")
            ):
                status = thermal.protection_status(self.settings, self.now)
            self.assertEqual(status["cooling"]["health"], expected)
            self.assertEqual(json.loads(path.read_text())["active_guid"], active)
        path.unlink()
        self.assertEqual(
            thermal.protection_status(self.settings, self.now)["cooling"]["health"], "unavailable"
        )

    def test_off_by_default_and_no_implicit_configuration(self):
        self.assertFalse(thermal.load_settings().enabled)
        self.assertIsNone(thermal.admission_reason())
        self.assertFalse(thermal.settings_path().exists())

    def test_missing_settings_in_existing_public_directory_remain_off_and_unchanged(self):
        parent = thermal.settings_path().parent
        parent.mkdir(mode=0o755, parents=True)
        parent.chmod(0o755)
        self.assertFalse(thermal.load_settings().enabled)
        self.assertEqual(parent.stat().st_mode & 0o777, 0o755)
        thermal.save_settings(self.settings)
        self.assertEqual(parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(thermal.load_settings(), self.settings)

    def test_explicit_save_never_chmods_or_follows_a_symlink_directory(self):
        target = self.root / "unrelated"
        target.mkdir(mode=0o755)
        target.chmod(0o755)
        parent = thermal.settings_path().parent
        parent.parent.mkdir(parents=True)
        parent.symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            thermal.save_settings(self.settings)
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)
        self.assertFalse((target / "thermal-settings.json").exists())

    def test_strict_configuration(self):
        for values in (
            {"enabled": 1},
            {"warning_c": 95},
            {"queue_c": float("nan")},
            {"batch_budget_percent": 0},
            {"sensor_file": "relative"},
            {"enabled": True},
            {"max_sample_age": True},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                thermal.ThermalSettings(**values)

    def test_fresh_verified_cpu_samples_and_invalid_or_stale_data(self):
        for value, level in ((70, "ok"), (80, "warning"), (85, "hot"), (90, "critical")):
            self.sample(value)
            self.assertEqual(thermal.read_temperature(self.settings, self.now)["level"], level)
        for value, stamp, source in (
            (True, 1000, "LibreHardwareMonitor"),
            (float("nan"), 1000, "LibreHardwareMonitor"),
            (0, 1000, "LibreHardwareMonitor"),
            (80, 980, "LibreHardwareMonitor"),
            (80, 1006, "LibreHardwareMonitor"),
            (80, 1000, "ACPI"),
        ):
            self.sample(value, stamp, source=source)
            self.assertEqual(
                thermal.read_temperature(self.settings, self.now)["level"], "unavailable"
            )
        self.sensor.write_text("{" * 65537)
        self.assertEqual(thermal.read_temperature(self.settings, self.now)["level"], "unavailable")

    def test_admission_waits_on_hot_or_missing_samples_but_not_monitor_only(self):
        write_private_json(thermal.settings_path(), thermal.asdict(self.settings))
        self.heartbeat()
        with (
            patch.object(thermal.time, "time", return_value=self.now),
            patch.object(thermal.time, "monotonic", return_value=self.now),
        ):
            self.assertIn("unavailable", thermal.admission_reason())
            self.sample(87)
            self.assertIn("hot", thermal.admission_reason())
            self.sample(70)
            self.assertIsNone(thermal.admission_reason())
        write_private_json(
            thermal.settings_path(), {**thermal.asdict(self.settings), "enforce": False}
        )
        self.sensor.unlink()
        self.assertIsNone(thermal.admission_reason())

    def test_critical_budget_is_shared_only_by_registered_batch_scopes(self):
        self.sample()
        store = self.store(("agent", "batch", "batch"))
        with patch.object(thermal, "manager", side_effect=self.manager):
            status = thermal.Controller(self.root).tick(self.settings, store, self.now)
        self.assertNotIn("scope-0", self.quotas)
        self.assertEqual(self.quotas, {"scope-1": 1000000, "scope-2": 1000000})
        self.assertEqual(status["cooling_jobs"], 2)
        self.assertTrue(
            all(call.kwargs.get("require_batch") for call in store.validate.call_args_list)
        )

    def test_stale_identity_and_unknown_or_stronger_quota_are_untouched(self):
        self.sample()
        store = self.store()
        store.validate.return_value = False
        with patch.object(thermal, "manager", side_effect=self.manager):
            thermal.Controller(self.root).tick(self.settings, store, self.now)
        self.assertEqual(self.commands, [])
        store.validate.return_value = True
        self.quotas["scope-0"] = 500000
        with patch.object(thermal, "manager", side_effect=self.manager):
            thermal.Controller(self.root).tick(self.settings, store, self.now)
        self.assertEqual(self.quotas["scope-0"], 500000)
        self.assertFalse(any(args[0] == "set-property" for args in self.commands))
        self.assertIsNone(thermal.quota_us("unknown"))

    def test_recovery_requires_cool_dwell_and_preserves_operator_changes(self):
        self.sample()
        store = self.store()
        controller = thermal.Controller(self.root)
        with patch.object(thermal, "manager", side_effect=self.manager):
            controller.tick(self.settings, store, self.now)
            self.sample(70, self.now + 5)
            controller.tick(self.settings, store, self.now + 5)
            self.assertEqual(self.quotas["scope-0"], 2000000)
            self.sample(70, self.now + 66)
            controller.tick(self.settings, store, self.now + 66)
            self.assertTrue(math.isinf(self.quotas["scope-0"]))
            self.sample()
            controller.tick(self.settings, store, self.now)
            self.quotas["scope-0"] = 100000
            controller.release(store)
            self.assertEqual(self.quotas["scope-0"], 100000)

    def test_unknown_sample_retains_existing_cooling_and_blocks_new_batch(self):
        self.sample()
        store = self.store()
        controller = thermal.Controller(self.root)
        with patch.object(thermal, "manager", side_effect=self.manager):
            controller.tick(self.settings, store, self.now)
            self.sensor.unlink()
            status = controller.tick(self.settings, store, self.now + 30)
            self.assertEqual(status["level"], "unavailable")
            self.assertEqual(self.quotas["scope-0"], 2000000)

    def test_rollback_is_persisted_before_any_cpu_mutation(self):
        self.sample()
        with (
            patch.object(thermal, "write_private_json", side_effect=OSError("disk full")),
            patch.object(thermal, "manager", side_effect=self.manager),
            self.assertRaises(OSError),
        ):
            thermal.Controller(self.root).tick(self.settings, self.store(), self.now)
        self.assertFalse(any(args[0] == "set-property" for args in self.commands))

    def test_missing_samples_restart_the_sustained_cooling_period(self):
        self.sample()
        store = self.store()
        controller = thermal.Controller(self.root)
        with patch.object(thermal, "manager", side_effect=self.manager):
            controller.tick(self.settings, store, self.now)
            self.sample(70, self.now + 5)
            controller.tick(self.settings, store, self.now + 5)
            self.sensor.unlink()
            controller.tick(self.settings, store, self.now + 30)
            self.sample(70, self.now + 66)
            controller.tick(self.settings, store, self.now + 66)
            self.assertEqual(self.quotas["scope-0"], 2000000)
            self.sample(70, self.now + 127)
            controller.tick(self.settings, store, self.now + 127)
            self.assertTrue(math.isinf(self.quotas["scope-0"]))

    def test_quota_units(self):
        for raw, expected in (
            ("infinity", math.inf),
            ("2s", 2000000),
            ("500ms", 500000),
            ("1000000", 1000000),
            ("0", None),
            ("nan", None),
        ):
            self.assertEqual(thermal.quota_us(raw), expected)

    def test_operator_mask_refuses_before_service_writes_or_manager_calls(self):
        path = self.root / "config/systemd/user/codex-thermal.service"
        path.parent.mkdir(parents=True)
        path.symlink_to("/dev/null")
        with patch.object(thermal, "manager") as manager, self.assertRaises(ValueError):
            thermal.install_service()
        manager.assert_not_called()
        self.assertTrue(path.is_symlink())

    def test_empty_and_data_control_masks_refuse_before_any_manager_calls(self):
        for location in (
            self.root / "config/systemd/user.control",
            self.root / "home/.local/share/systemd/user",
            self.root / "extra-units",
        ):
            with (
                self.subTest(location=location),
                patch.dict(
                    os.environ,
                    {
                        "HOME": str(self.root / "home"),
                        "SYSTEMD_UNIT_PATH": str(self.root / "extra-units"),
                    },
                ),
            ):
                location.mkdir(parents=True, exist_ok=True)
                mask = location / "codex-thermal.service"
                mask.touch()
                with patch.object(thermal, "manager") as manager, self.assertRaises(ValueError):
                    thermal.precheck_service()
                manager.assert_not_called()
                mask.unlink()

    def test_failed_budget_application_can_be_retried_from_its_durable_journal(self):
        self.sample()
        store = self.store()
        with patch.object(thermal, "manager", side_effect=self.manager):
            controller = thermal.Controller(self.root)
            with patch.object(controller, "_set_quota", return_value=False):
                controller.tick(self.settings, store, self.now)
            self.assertEqual(self.quotas, {})
            restarted = thermal.Controller(self.root)
            restarted.tick(self.settings, store, self.now)
            self.assertEqual(self.quotas["scope-0"], 2000000)
