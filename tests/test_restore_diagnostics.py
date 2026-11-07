from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from codex_looper.restore_diagnostics import (
    LOG_COUNT,
    MAX_LOG_AGE_SECONDS,
    append_record,
    main,
    resource_snapshot,
)


class RestoreDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.logs = self.root / "diagnostics"

    def test_rotation_bounds_every_file_and_retains_latest_record(self):
        for number in range(100):
            self.assertTrue(
                append_record(self.logs, {"number": number, "padding": "x" * 90}, max_bytes=512)
            )
        files = list(self.logs.glob("restore.jsonl*"))
        self.assertEqual(len(files), LOG_COUNT)
        self.assertLessEqual(sum(path.stat().st_size for path in files), LOG_COUNT * 512)
        for path in files:
            self.assertLessEqual(path.stat().st_size, 512)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            for line in path.read_text().splitlines():
                json.loads(line)
        current = [
            json.loads(line) for line in (self.logs / "restore.jsonl").read_text().splitlines()
        ]
        self.assertEqual(current[-1]["number"], 99)
        self.assertEqual(self.logs.stat().st_mode & 0o777, 0o700)

    def test_concurrent_writers_keep_rotation_bounded_and_records_intact(self):
        def write(number):
            return append_record(self.logs, {"number": number, "padding": "x" * 90}, max_bytes=512)

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(write, range(100)))
        self.assertTrue(any(results))
        files = list(self.logs.glob("restore.jsonl*"))
        self.assertLessEqual(len(files), LOG_COUNT)
        self.assertLessEqual(sum(path.stat().st_size for path in files), LOG_COUNT * 512)
        for path in files:
            for line in path.read_text().splitlines():
                self.assertIn("number", json.loads(line))

    def test_oversized_records_are_dropped_and_existing_oversized_files_are_repaired(self):
        self.assertFalse(append_record(self.logs, {"padding": "x" * 1024}, max_bytes=512))
        self.assertFalse(self.logs.exists())
        self.logs.mkdir()
        for name in ("restore.jsonl", "restore.jsonl.1", "restore.jsonl.2"):
            (self.logs / name).write_bytes(b"x" * 1024)
        self.assertTrue(append_record(self.logs, {"event": "exit"}, max_bytes=512))
        self.assertLessEqual(
            sum(path.stat().st_size for path in self.logs.iterdir()), 512 * LOG_COUNT
        )

    def test_log_symlinks_do_not_modify_another_file(self):
        self.logs.mkdir()
        target = self.root / "private-data"
        target.write_text("preserve this file")
        (self.logs / "restore.jsonl").symlink_to(target)
        with self.assertRaises(ValueError):
            append_record(self.logs, {"event": "start"})
        self.assertEqual(target.read_text(), "preserve this file")

    def test_age_pruning_uses_first_record_even_after_newer_appends(self):
        old = datetime.fromtimestamp(time.time() - MAX_LOG_AGE_SECONDS - 60, timezone.utc)
        self.assertTrue(append_record(self.logs, {"time": old.isoformat(), "old": True}))
        self.assertTrue(append_record(self.logs, {"event": "start"}))
        current = (self.logs / "restore.jsonl").read_text()
        self.assertNotIn('"old"', current)
        self.assertEqual(json.loads(current), {"event": "start"})

    def test_process_scan_has_a_fixed_budget_and_reports_incomplete_counts(self):
        proc = self.root / "proc"
        for number in range(4):
            (proc / str(number)).mkdir(parents=True)
        with patch("codex_looper.restore_diagnostics.MAX_PROCESSES", 2):
            snapshot = resource_snapshot(proc)
        self.assertEqual(snapshot["processes"]["total"], 2)
        self.assertTrue(snapshot["process_scan_truncated"])

    def test_busy_lock_cannot_stall_a_restore(self):
        self.logs.mkdir()
        with (self.logs / ".restore.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            started = time.monotonic()
            self.assertFalse(append_record(self.logs, {"event": "start"}))
            self.assertLess(time.monotonic() - started, 2)

    def test_records_use_only_counters_and_fixed_fields_not_user_data(self):
        proc = self.root / "proc"
        (proc / "123").mkdir(parents=True)
        (proc / "meminfo").write_text("MemTotal: 8192000 kB\nMemAvailable: 4096000 kB\n")
        (proc / "123/comm").write_text("codex\n")
        (proc / "123/status").write_text("VmRSS: 2048 kB\n")
        (proc / "123/cmdline").write_text("private-conversation --api-key secret-token")
        with patch.dict(
            os.environ,
            {
                "CODEX_PROC_ROOT": str(proc),
                "XDG_STATE_HOME": str(self.root),
                "API_KEY": "secret-token",
                "CODEX_SESSION": "private-farm",
            },
        ):
            self.assertEqual(
                main(
                    [
                        "start",
                        "--run-id",
                        "123-45-67",
                        "--phase",
                        "farm",
                        "--pid",
                        "123",
                        "--memory-policy",
                        "warn",
                    ]
                ),
                0,
            )
        path = self.root / "codexfarm/diagnostics/restore.jsonl"
        content = path.read_text()
        for private in ("private-conversation", "secret-token", "private-farm", str(self.root)):
            self.assertNotIn(private, content)
        record = json.loads(content)
        self.assertEqual(record["resources"]["memory"]["available_mib"], 4000)
        self.assertEqual(record["resources"]["rss_mib"]["codex"], 2)
        self.assertNotIn("tmux", record)


if __name__ == "__main__":
    unittest.main()
