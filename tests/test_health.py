from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_looper.health import (
    HealthMonitor,
    Limits,
    Memory,
    backup_issues,
    backups_expected,
    main,
    read_memory,
    refresh_memory_title,
    shared_codex_servers,
    tree_memory,
)


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def test_restore_check_is_advisory_unless_enforced(self):
        for memory in (Memory(8000, 400, 0), None):
            for enforce in (False, True):
                with (
                    self.subTest(memory=memory, enforce=enforce),
                    patch(
                        "sys.argv",
                        ["codex-health", "--restore-check"]
                        + (["--enforce-memory-pressure"] if enforce else []),
                    ),
                    patch("codex_looper.health.read_memory", return_value=memory),
                ):
                    self.assertEqual(main(), 3 if enforce and memory else 0)

    def test_enforce_memory_requires_restore_check(self):
        with patch("sys.argv", ["codex-health", "--enforce-memory-pressure"]):
            with self.assertRaises(SystemExit) as result:
                main()
            self.assertEqual(result.exception.code, 2)

    def test_explicit_disabled_archive_choices_override_stale_watcher(self):
        self.prepare_backup()
        (self.root / "backup-watch.json").write_text('{"archive_enabled": true}')
        with patch.dict(os.environ, {"CODEXFARM_BACKUP_HEALTH_ENABLED": "0"}):
            for name in ("autoservice_choice", "conversation_backup_choice"):
                choice = self.root / name
                choice.write_text("no\n")
                self.assertFalse(backups_expected(self.root))
                self.assertEqual(
                    backup_issues(self.root, 18000, expected=backups_expected(self.root)), []
                )
                with patch.dict(os.environ, {"CODEXFARM_BACKUP_HEALTH_ENABLED": "1"}):
                    self.assertTrue(backups_expected(self.root))
                choice.unlink()
        self.assertTrue((self.root / "backup-status.json").exists())
        self.assertTrue((self.root / "backup-watch.json").exists())

    def test_scheduled_archives_need_separate_consent(self):
        with patch.dict(os.environ, {"CODEXFARM_BACKUP_HEALTH_ENABLED": "0"}):
            (self.root / "autoservice_choice").write_text("yes\n")
            self.assertFalse(backups_expected(self.root))
            (self.root / "conversation_backup_choice").write_text("yes\n")
            self.assertTrue(backups_expected(self.root))
            self.assertTrue(backup_issues(self.root, 10000, expected=backups_expected(self.root)))

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

    def test_default_headroom_does_not_scale_with_ram(self):
        for total in (4096, 8192, 65536):
            for available, expected in ((1400, "warning"), (1024, "critical"), (2048, "ok")):
                with self.subTest(total=total, available=available):
                    self.assertEqual(Memory(total, available, 0).level(Limits()), expected)
        self.assertEqual(Memory(8192, 1536, 0).level(Limits()), "warning")
        self.assertEqual(Memory(8192, 1537, 0).level(Limits()), "ok")
        self.assertEqual(Memory(8192, 1025, 0).level(Limits()), "warning")

    def test_legacy_percentage_overrides_select_percent_policy(self):
        for values in (
            {"CODEXFARM_MEMORY_WARN_PERCENT": "20"},
            {"CODEXFARM_MEMORY_CRITICAL_PERCENT": "10"},
            {"CODEXFARM_MEMORY_POLICY": "percent"},
        ):
            with self.subTest(values=values), patch.dict(os.environ, values, clear=True):
                limits = Limits.from_env()
                self.assertEqual(Memory(65536, 2048, 0).level(limits), "critical")
                self.assertEqual(Memory(4096, 1400, 0).level(limits), "ok")

    def test_explicit_headroom_policy_wins_over_percent_overrides(self):
        with patch.dict(
            os.environ,
            {
                "CODEXFARM_MEMORY_POLICY": "headroom",
                "CODEXFARM_MEMORY_WARN_PERCENT": "40",
                "CODEXFARM_MEMORY_CRITICAL_PERCENT": "30",
                "CODEXFARM_MEMORY_WARN_MIB": "1800",
                "CODEXFARM_MEMORY_CRITICAL_MIB": "1200",
            },
            clear=True,
        ):
            limits = Limits.from_env()
            self.assertEqual(Memory(65536, 1800, 0).level(limits), "warning")
            self.assertEqual(Memory(65536, 1200, 0).level(limits), "critical")
            self.assertEqual(Memory(65536, 2048, 0).level(limits), "ok")

    def test_limits_preserve_existing_positional_fields(self):
        limits = Limits(30, 15, 2000)
        self.assertEqual(
            (limits.warning_percent, limits.critical_percent, limits.session_mib), (30, 15, 2000)
        )

    def test_invalid_headroom_and_policy_configuration_fail_clearly(self):
        for values in (
            {"CODEXFARM_MEMORY_POLICY": "other"},
            {"CODEXFARM_MEMORY_WARN_MIB": "nan"},
            {"CODEXFARM_MEMORY_CRITICAL_MIB": "inf"},
            {"CODEXFARM_MEMORY_WARN_MIB": "1024"},
            {"CODEXFARM_MEMORY_CRITICAL_MIB": "-1"},
            {"CODEXFARM_MEMORY_POLICY": "headroom", "CODEXFARM_MEMORY_WARN_PERCENT": "nan"},
            {
                "CODEXFARM_MEMORY_POLICY": "headroom",
                "CODEXFARM_MEMORY_WARN_PERCENT": "10",
                "CODEXFARM_MEMORY_CRITICAL_PERCENT": "20",
            },
        ):
            with self.subTest(values=values), patch.dict(os.environ, values, clear=True):
                with self.assertRaises(ValueError):
                    Limits.from_env()
        for values in ((True, 10, 1024), (20, 10, float("nan")), (10, 20, 1024)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Limits(*values)

    def test_io_pressure_is_reported_separately_and_does_not_refuse_restore(self):
        (self.root / "meminfo").write_text("MemTotal: 8388608 kB\nMemAvailable: 4194304 kB\n")
        self.assertEqual(getattr(read_memory(self.root), "io_pressure_percent", None), 0)
        (self.root / "pressure").mkdir()
        (self.root / "pressure/io").write_text("some avg10=95.50 avg60=2 total=4\n")
        memory = read_memory(self.root)
        self.assertEqual(memory.io_pressure_percent, 95.5)
        self.assertEqual(memory.pressure_percent, 0)
        self.assertIn("I/O stalls 95.5%", memory.description())
        with (
            patch("sys.argv", ["codex-health", "--restore-check", "--enforce-memory-pressure"]),
            patch("codex_looper.health.read_memory", return_value=memory),
        ):
            self.assertEqual(main(), 0)
        for value in ("nan", "inf", "-1", "101", "bad"):
            with self.subTest(value=value):
                (self.root / "pressure/io").write_text(f"some avg10={value}\n")
                self.assertEqual(read_memory(self.root).io_pressure_percent, 0)

    def test_invalid_direct_memory_readings_are_unknown(self):
        for memory in (
            Memory(0, 0, 0),
            Memory(8192, -1, 0),
            Memory(8192, 9000, 0),
            Memory(float("nan"), 2000, 0),
            Memory(8192, 2000, 0, float("inf")),
        ):
            with self.subTest(memory=memory):
                self.assertEqual(memory.level(Limits()), "unavailable")

    def test_missing_or_malformed_memory_is_unknown(self):
        self.assertIsNone(read_memory(self.root))
        (self.root / "meminfo").write_text("MemTotal: oops\n")
        self.assertIsNone(read_memory(self.root))

    def test_overflowing_memory_counters_are_unknown(self):
        (self.root / "meminfo").write_text(f"MemTotal: {'9' * 400} kB\nMemAvailable: 4096000 kB\n")
        self.assertIsNone(read_memory(self.root))

    def test_whole_tree_includes_test_workers_without_double_counting_panes(self):
        values = tree_memory(
            "1 0 1024\n2 1 2048\n3 2 4096\n4 0 512\ninvalid\n",
            {"@a": {1, 2}, "@b": {4}},
        )
        self.assertEqual(values, {"@a": 7, "@b": 0.5})

    def test_shared_server_tree_is_not_attributed_to_its_parent_pane(self):
        processes = "10 0 1024 bash\n11 10 2048 codex\n20 11 3072000 codex\n21 20 1024000 worker\n30 0 4096 codex\n"
        self.assertEqual(
            tree_memory(processes, {"@a": {10}, "@b": {30}}, excluded={20}), {"@a": 3, "@b": 4}
        )
        self.assertEqual(tree_memory(processes, {"shared": {20}}), {"shared": 4000})

    def test_shared_server_detection_checks_subcommand_and_managed_daemon_flag(self):
        proc = self.root / "proc/20"
        proc.mkdir(parents=True)
        processes = "20 11 3072000 codex\n"
        for argv, expected in (
            (["/usr/bin/codex", "app-server", "--managed-daemon"], {20}),
            (["/usr/bin/codex", "--profile", "work", "app-server", "--managed-daemon"], {20}),
            (["/usr/bin/codex", "--config", "label=app-server", "exec", "--managed-daemon"], set()),
            (["/usr/bin/codex", "app-server", "--stdio"], set()),
            (["/usr/bin/codex", "resume", "--managed-daemon"], set()),
        ):
            with (
                self.subTest(argv=argv),
                patch.dict(os.environ, {"CODEX_PROC_ROOT": str(proc.parent)}),
            ):
                (proc / "cmdline").write_bytes(b"\0".join(value.encode() for value in argv) + b"\0")
                self.assertEqual(shared_codex_servers(processes), expected)

    def test_monitor_reports_shared_server_memory_separately_from_panes(self):
        proc = self.root / "proc/20"
        proc.mkdir(parents=True)
        (proc / "cmdline").write_bytes(b"/usr/bin/codex\0app-server\0--managed-daemon\0")
        processes = "10 0 1024 bash\n11 10 2048 codex\n20 11 3072000 codex\n21 20 1024000 worker\n"
        calls = []

        def tmux(command):
            calls.append(command)
            return "$0\t@1\t10\t\tproject\n" if command[1] == "list-panes" else ""

        with (
            patch.dict(os.environ, {"CODEX_PROC_ROOT": str(proc.parent)}),
            patch("codex_looper.health.read_memory", return_value=Memory(8000, 4000, 0)),
            patch(
                "codex_looper.health.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, processes),
            ),
        ):
            monitor = HealthMonitor(self.root, Limits())
            monitor.tick({"@1"}, tmux, now=10000)
            monitor.tick({"@1"}, tmux, now=10020)
        status = json.loads((self.root / "health-status.json").read_text())
        self.assertEqual(status["windows_mib"], {"@1": 3})
        self.assertEqual(status["shared_servers_mib"], 4000)
        self.assertFalse(any("LARGE CHAT" in str(call) for call in calls))

    def test_doctor_shared_server_rss_is_informational_with_healthy_host_memory(self):
        (self.root / "health-status.json").write_text(
            json.dumps(
                {"checked_at": time.time(), "windows_mib": {"@1": 100}, "shared_servers_mib": 4000}
            )
        )
        with (
            patch("sys.argv", ["codex-health"]),
            patch("codex_looper.health.state_directory", return_value=self.root),
            patch("codex_looper.health.read_memory", return_value=Memory(8000, 4000, 0)),
        ):
            self.assertEqual(main(), 0)

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

    def test_disabled_backup_checks_ignore_stale_manual_archives(self):
        self.prepare_backup()
        self.assertEqual(backup_issues(self.root, 18000, expected=False), [])

    def test_registered_farm_does_not_require_full_archives(self):
        config = self.root / "config/codexfarm"
        config.mkdir(parents=True)
        (config / "farms.tsv").write_text("session\tmanifest\nwork\twork.tsv\n")
        with (
            patch.dict(
                os.environ,
                {
                    "XDG_CONFIG_HOME": str(config.parent),
                    "XDG_STATE_HOME": str(self.root / "state"),
                    "CODEXFARM_BACKUP_HEALTH_ENABLED": "0",
                },
            ),
            patch("sys.argv", ["codex-health"]),
            patch("codex_looper.health.read_memory", return_value=Memory(8000, 4000, 0)),
        ):
            self.assertEqual(main(), 0)

    def test_archive_health_requires_explicit_monitoring_or_archive_watcher(self):
        marker = self.root / "backup-watch.json"
        with patch.dict(os.environ, {"CODEXFARM_BACKUP_HEALTH_ENABLED": "0"}):
            self.assertFalse(backups_expected(self.root))
            marker.write_text(json.dumps({"pid": 123, "archive_enabled": False}))
            self.assertFalse(backups_expected(self.root))
            marker.write_text(json.dumps({"pid": 123}))
            self.assertFalse(backups_expected(self.root))
            marker.write_text(json.dumps({"pid": 123, "archive_enabled": True}))
            self.assertTrue(backups_expected(self.root))
        marker.unlink()
        with patch.dict(os.environ, {"CODEXFARM_BACKUP_HEALTH_ENABLED": "1"}):
            self.assertTrue(backups_expected(self.root))
        self.assertIn("--archive", backup_issues(self.root, 10000, expected=True)[0])

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

    def test_memory_labels_refresh_clear_and_reappear_only_for_opted_in_windows(self):
        calls = []
        names = {"@1": "*200+MB** *RUN* project", "@2": "native title"}

        def tmux(command):
            calls.append(command)
            if command[1] == "list-panes":
                return (
                    f"$1\t@1\t123\t200\t{names['@1']}\n"
                    f"$1\t@2\t456\t\t{names['@2']}\n"
                    "$1\t@3\t789\t200\tignored window\n"
                )
            if command[1] == "rename-window":
                names[command[3]] = command[4]
            return ""

        monitor = HealthMonitor(self.root, Limits())
        with (
            patch("codex_looper.health.read_memory", return_value=Memory(8000, 4000, 0)),
            patch("codex_looper.health.subprocess.run") as processes,
            patch("codex_looper.health.config_directory", return_value=self.root),
        ):
            for now, rss, expected in (
                (10000, 307200, "*300.0MB** *RUN* project"),
                (10015, 256000, "*250.0MB** *RUN* project"),
                (10030, 102400, "*RUN* project"),
                (10045, 409600, "*400.0MB** *RUN* project"),
            ):
                processes.return_value = subprocess.CompletedProcess(
                    [], 0, f"123 0 {rss}\n456 0 900000\n789 0 900000\n"
                )
                monitor.tick({"@1", "@2"}, tmux, now=now)
                self.assertEqual(names["@1"], expected)
                self.assertEqual(names["@2"], "native title")
            calls.clear()
            monitor.tick({"@1", "@2"}, tmux, now=10060)
            self.assertFalse(any(call[1] == "rename-window" for call in calls))
            processes.side_effect = OSError
            monitor.tick({"@1", "@2"}, tmux, now=10075)
            self.assertEqual(names["@1"], "*400.0MB** *RUN* project")
        self.assertFalse(any("@3" in call for call in calls))

    def test_invalid_memory_title_options_leave_native_titles_untouched(self):
        for threshold in ("", "nan", "inf", "-1", "0", "bad"):
            with self.subTest(threshold=threshold):
                calls = []
                refresh_memory_title(calls.append, "@1", "native title", threshold, 300)
                self.assertEqual(calls, [])

    def test_memory_title_uses_custom_cutoff_not_the_cutoff_as_its_value(self):
        calls = []
        refresh_memory_title(calls.append, "@1", "*512+MB** *READY* project", "1024", 1500.125)
        self.assertIn(["tmux", "rename-window", "-t", "@1", "*1500.1MB** *READY* project"], calls)
        calls.clear()
        refresh_memory_title(calls.append, "@1", "*1500.1MB** *READY* project", "1024", 600)
        self.assertIn(["tmux", "rename-window", "-t", "@1", "*READY* project"], calls)

    def test_doctor_detects_dead_monitor_by_heartbeat(self):
        (self.root / "managed_sessions").touch()
        with (
            patch("sys.argv", ["codex-health"]),
            patch("codex_looper.health.state_directory", return_value=self.root),
            patch("codex_looper.health.config_directory", return_value=self.root),
            patch("codex_looper.health.read_memory", return_value=Memory(8000, 4000, 0)),
        ):
            self.assertEqual(main(), 1)
