import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_looper.pane_logs import prune_logs, write_stream


class PaneLogTests(unittest.TestCase):
    def test_long_stream_retains_recent_output_with_bounded_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pane.log"
            path.touch(mode=0o600)
            payload = b"old output\n" * 100000 + b"final output\n"
            write_stream(path, io.BytesIO(payload), limit=131072, tail=32768)
            self.assertLessEqual(path.stat().st_size, 131072)
            self.assertTrue(path.read_bytes().endswith(b"final output\n"))

    def test_active_old_log_and_unknown_names_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inactive = root / "agent_20200101-000000_token.log"
            active = root / "agent_20200101-000000_active.log"
            unknown = root / "personal.log"
            for path in (inactive, active, unknown):
                path.write_bytes(b"evidence")
                os.utime(path, (1, 1))
            with patch(
                "codex_looper.pane_logs.active_log_paths", return_value=({str(active)}, False)
            ):
                report = prune_logs(root, apply=True)
            self.assertEqual(report["removed"], [str(inactive)])
            self.assertTrue(active.exists())
            self.assertTrue(unknown.exists())
