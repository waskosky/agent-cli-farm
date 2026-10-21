"""Aggregate history is optional, private, bounded and free of recipe data."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_looper.resource_history import ResourceHistory


class HistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o700)
        self.history = ResourceHistory(self.root)

    def sample(self, at=1000, **updates):
        value = {
            "checked_at": at,
            "level": "ok",
            "cpu_celsius": 60,
            "effective_enforcement": True,
            "cooling_jobs": 0,
            "issues": [],
            "argv": ["secret"],
            "cwd": "/secret",
            "env": {"TOKEN": "secret"},
        }
        value.update(updates)
        return value

    def record(self, at=1000, **updates):
        self.history.record(self.sample(at, **updates), hours=1, max_bytes=4096)

    def test_disabled_history_does_not_create_files_or_read_counters(self):
        with patch("codex_looper.health.read_memory", side_effect=AssertionError("off")):
            self.history.record(self.sample(), hours=0, max_bytes=4096)
        self.assertFalse(self.history.path.exists())

    def test_downsample_normal_samples_but_keep_transitions_and_exclude_private_fields(self):
        self.record()
        self.record(1005)
        self.record(1010, level="warning")
        values = [json.loads(line) for line in self.history.path.read_text().splitlines()]
        self.assertEqual([item["checked_at"] for item in values], [1000, 1010])
        self.assertNotIn("secret", self.history.path.read_text())
        self.assertEqual(self.history.path.stat().st_mode & 0o777, 0o600)

    def test_byte_and_age_limits_keep_recent_records_across_restarts(self):
        for at in range(1000, 12000, 60):
            self.record(at)
        self.assertLessEqual(self.history.path.stat().st_size, 4096)
        values = [json.loads(line) for line in self.history.path.read_text().splitlines()]
        self.assertTrue(all(value["checked_at"] >= 11980 - 3600 for value in values))
        restarted = ResourceHistory(self.root)
        restarted.record(self.sample(12000), hours=1, max_bytes=4096)
        self.assertLessEqual(restarted.path.stat().st_size, 4096)

    def test_corrupt_or_symlink_history_is_preserved(self):
        self.history.path.write_text("broken")
        self.history.path.chmod(0o600)
        with self.assertRaises(ValueError):
            self.record()
        self.assertEqual(self.history.path.read_text(), "broken")
        self.history.path.unlink()
        target = self.root / "unrelated"
        target.write_text("preserve")
        self.history.path.symlink_to(target)
        with self.assertRaises(OSError):
            self.record()
        self.assertEqual(target.read_text(), "preserve")
