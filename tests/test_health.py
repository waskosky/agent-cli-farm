from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_looper.health import (
    HealthMonitor,
    Limits,
    Memory,
    backup_issues,
    main,
    read_memory,
    tree_memory,
)


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def test_available_memory_and_pressure_drive_alerts_not_old_swap(self):
        (self.root / "meminfo").write_text(
            "MemTotal: 8192000 kB\nMemAvailable: 4096000 kB\n"
            "SwapTotal: 4096000 kB\nSwapFree: 0 kB\n"
        )
        memory = read_memory(self.root)
        self.assertEqual(memory.level(Limits()), "ok")
        self.assertEqual(memory.swap_used_mib, 4000)
        (self.root / "pressure").mkdir()
        (self.root / "pressure/memory").write_text("some avg10=26.50 avg60=2 total=4\n")
        self.assertEqual(read_memory(self.root).level(Limits()), "critical")
        self.assertEqual(Memory(8000, 1500, 0).level(Limits()), "warning")
        self.assertEqual(Memory(8000, 799, 0).level(Limits()), "critical")

    def test_missing_or_malformed_memory_is_unknown(self):
        self.assertIsNone(read_memory(self.root))
        (self.root / "meminfo").write_text("MemTotal: oops\n")
        self.assertIsNone(read_memory(self.root))

    def test_whole_tree_includes_test_workers_without_double_counting_panes(self):
        values = tree_memory(
            "1 0 1024\n2 1 2048\n3 2 4096\n4 0 512\ninvalid\n",
            {"@a": {1, 2}, "@b": {4}},
        )
        self.assertEqual(values, {"@a": 7, "@b": 0.5})

    def test_invalid_thresholds_fail_cleanly(self):
        for values in (
            {"CODEXFARM_MEMORY_WARN_PERCENT": "nan"},
            {"CODEXFARM_MEMORY_WARN_PERCENT": "10", "CODEXFARM_MEMORY_CRITICAL_PERCENT": "20"},
            {"CODEXFARM_MEMORY_SESSION_MIB": "-1"},
        ):
            with self.subTest(values=values), patch.dict(os.environ, values):
                with self.assertRaises(ValueError):
                    Limits.from_env()

    def prepare_backup(self, now=10000):
        destination = self.root / "backups"
        destination.mkdir()
        (destination / "archive.tar.gz").touch()
        (destination / "latest.json").write_text(
            json.dumps({"archive": "archive.tar.gz", "created_at": now})
        )
        (self.root / "backup-status.json").write_text(
            json.dumps(
                {
                    "checked_at": now,
                    "manifest_save_ok": True,
                    "backup_ok": True,
                }
            )
        )
        return destination

    def test_backup_health_checks_result_freshness_and_actual_archive(self):
        self.assertTrue(backup_issues(self.root, 10000, expected=True))
        self.assertFalse(backup_issues(self.root, 10000, expected=False))
        destination = self.prepare_backup()
        self.assertEqual(backup_issues(self.root, 10000, expected=True), [])
        stale = backup_issues(self.root, 18000, expected=True)
        self.assertTrue(any("heartbeat" in issue for issue in stale))
        self.assertTrue(any("2 hours" in issue for issue in stale))
        (destination / "archive.tar.gz").unlink()
        self.assertIn(
            "conversation backup archive is missing", backup_issues(self.root, 10000, expected=True)
        )

    def test_failed_save_is_visible_even_when_history_backup_succeeds(self):
        self.prepare_backup()
        (self.root / "backup-status.json").write_text(
            json.dumps(
                {
                    "checked_at": 10000,
                    "manifest_save_ok": False,
                    "backup_ok": True,
                }
            )
        )
        issues = backup_issues(self.root, 10000, expected=True)
        self.assertEqual(issues, ["latest exact session manifest save failed or was skipped"])

    def test_monitor_sustains_warnings_throttles_notifications_and_prunes_windows(self):
        calls = []

        def tmux(command):
            calls.append(command)
            if command[1] == "list-panes":
                return "$1\t@1\t123\n$2\t@2\t789\n"
            if command[1] == "show-options":
                return "clock"
            return ""

        monitor = HealthMonitor(self.root, Limits())
        processes = subprocess.CompletedProcess([], 0, "123 0 2000000\n789 0 2000000\n")
        with (
            patch("codex_looper.health.read_memory", return_value=Memory(8000, 1400, 500)),
            patch("codex_looper.health.subprocess.run", return_value=processes),
            patch("codex_looper.health.config_directory", return_value=self.root),
        ):
            monitor.tick({"@1"}, tmux, now=10000)
            self.assertFalse(any(call[1] == "display-message" for call in calls))
            count = len(calls)
            monitor.tick({"@1"}, tmux, now=10005)
            self.assertEqual(len(calls), count)
            monitor.tick({"@1"}, tmux, now=10015)
            self.assertEqual(sum(call[1] == "display-message" for call in calls), 1)
            monitor.tick({"@1"}, tmux, now=10030)
            self.assertEqual(sum(call[1] == "display-message" for call in calls), 1)
            state = json.loads((self.root / "health-status.json").read_text())
            self.assertEqual(set(state["windows_mib"]), {"@1"})
            self.assertIn("RAM LOW", state["warnings"])
            self.assertEqual((self.root / "health-status.json").stat().st_mode & 0o777, 0o600)
            monitor.tick(set(), tmux, now=10045)
            self.assertEqual(
                json.loads((self.root / "health-status.json").read_text())["windows_mib"], {}
            )
        self.assertFalse(any(call[1].startswith("rename") for call in calls))
        self.assertTrue(any(call[-1].endswith("clock") for call in calls))
        self.assertFalse(any("@2" in call for call in calls if call[1] != "list-panes"))

    def test_monitor_reports_critical_on_first_sample(self):
        calls = []

        def tmux(command):
            calls.append(command)
            return "$1\t@1\t123\n" if command[1] == "list-panes" else ""

        with (
            patch("codex_looper.health.read_memory", return_value=Memory(8000, 400, 4000)),
            patch("codex_looper.health.subprocess.run", side_effect=OSError),
            patch("codex_looper.health.config_directory", return_value=self.root),
        ):
            HealthMonitor(self.root, Limits()).tick({"@1"}, tmux, now=10000)
        self.assertTrue(
            any(call[1] == "display-message" and "CRITICAL" in call[-1] for call in calls)
        )

    def test_doctor_detects_dead_monitor_by_heartbeat(self):
        (self.root / "managed_sessions").touch()
        with (
            patch("sys.argv", ["codex-health"]),
            patch("codex_looper.health.state_directory", return_value=self.root),
            patch("codex_looper.health.config_directory", return_value=self.root),
            patch("codex_looper.health.read_memory", return_value=Memory(8000, 4000, 0)),
        ):
            self.assertEqual(main(), 1)
