import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "bin" / "codex-manifest.py"
SESSION_ID = "019e1659-3a2f-7a40-95cf-5ac9dd7fe5d4"
HEADER = "name\tdir\tcmd\targs\n"


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = self.root / "manifest.tsv"

    def run_helper(self, *args):
        return subprocess.run(
            [sys.executable, str(HELPER), *map(str, args)], text=True, capture_output=True
        )

    def test_unchanged_manifest_refreshes_capture_without_rewriting_recovery_bytes(self):
        content = HEADER + "shell\t/tmp\tbash\t\n"
        self.manifest.write_text(content)
        os.utime(self.manifest, (1000, 1000))
        candidate = self.root / "candidate.tsv"
        candidate.write_text(content)
        result = self.run_helper("publish", candidate, self.manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.manifest.stat().st_mtime, 1000)
        receipt = self.manifest.with_name(self.manifest.name + ".capture.json")
        value = json.loads(receipt.read_text())
        self.assertTrue(value["complete"])
        self.assertLess(time.time() - value["checked_at"], 5)
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.run_helper("capture-check", self.manifest).returncode, 0)
        self.manifest.write_text(content.replace("shell", "changed"))
        self.assertNotEqual(self.run_helper("capture-check", self.manifest).returncode, 0)

    def test_partial_capture_keeps_coverage_failure_visible(self):
        candidate = self.root / "candidate.tsv"
        candidate.write_text(HEADER + "shell\t/tmp\tbash\t\n")
        result = self.run_helper(
            "publish", candidate, self.manifest, "--merge", "--unresolved", "2"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(
            self.manifest.with_name(self.manifest.name + ".capture.json").read_text()
        )
        self.assertFalse(receipt["complete"])
        self.assertEqual(receipt["unresolved_panes"], 2)
        self.assertNotEqual(self.run_helper("capture-check", self.manifest).returncode, 0)

    def test_restore_outcomes_are_bounded_private_and_attempts_are_per_manifest(self):
        self.manifest.write_text(
            HEADER + f"private-name\t/private/path\tcodex\tresume {SESSION_ID}\n"
        )
        outcomes = self.root / "outcomes.tsv"
        outcomes.write_text("1\tlaunch_failed\n")
        args = (
            "record-restore",
            self.manifest,
            "--normalized",
            self.manifest,
            "--outcomes",
            outcomes,
            "--exit-code",
            "1",
        )
        self.assertEqual(self.run_helper(*args).returncode, 0)
        path = self.manifest.with_name(self.manifest.name + ".restore.json")
        value = json.loads(path.read_text())
        self.assertFalse(value["complete"])
        self.assertEqual(value["attempt"], 1)
        for private in ("private-name", "/private/path", SESSION_ID):
            self.assertNotIn(private, path.read_text())
        outcomes.write_text("1\talready_present\n")
        self.assertEqual(self.run_helper(*args[:-1], "0").returncode, 0)
        value = json.loads(path.read_text())
        self.assertTrue(value["complete"])
        self.assertEqual(value["attempt"], 2)
        self.assertEqual(value["counts"], {"already_present": 1})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        outcomes.write_text("1\talready_present\n1\tlaunched\n")
        before = path.read_bytes()
        self.assertNotEqual(self.run_helper(*args).returncode, 0)
        self.assertEqual(path.read_bytes(), before)

    def test_history_retention_is_explicit_and_preserves_operator_files(self):
        candidate = self.root / "candidate.tsv"
        for index in range(4):
            candidate.write_text(HEADER + f"shell-{index}\t/tmp\tbash\t\n")
            self.assertEqual(self.run_helper("publish", candidate, self.manifest).returncode, 0)
        history = self.manifest.with_name(self.manifest.name + ".history")
        self.assertEqual(len(list(history.glob("*.tsv"))), 4)
        operator = history / ("20000101T000000.000000Z-" + "0" * 64 + ".tsv")
        operator.write_text("operator content")
        operator.chmod(0o600)
        result = self.run_helper("prune-history", self.manifest, "--keep", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        counts = json.loads(result.stdout)
        self.assertEqual(counts["pruned"], 2)
        self.assertEqual(operator.read_text(), "operator content")
        self.assertEqual(len(list(history.glob("*.tsv"))), 3)
        self.assertTrue(
            any(path.read_bytes() == self.manifest.read_bytes() for path in history.glob("*.tsv"))
        )

    def test_invalid_automatic_history_retention_fails_before_manifest_mutation(self):
        from unittest.mock import patch

        self.manifest.write_text(HEADER + "old\t/tmp\tbash\t\n")
        before = self.manifest.read_bytes()
        candidate = self.root / "candidate.tsv"
        candidate.write_text(HEADER + "new\t/tmp\tbash\t\n")
        with patch.dict(os.environ, {"CODEXFARM_MANIFEST_HISTORY_KEEP": "invalid"}):
            result = self.run_helper("publish", candidate, self.manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_validation_rejects_unsafe_or_malformed_rows(self):
        for row in (
            "proj\t/tmp\tcodex\tresume --last",
            'proj\t/tmp\t"codex resume --last"\t',
            "proj\t/tmp\t'claude --continue'\t",
            'proj\t/tmp\t"gemini --resume latest"\t',
            "proj\t/tmp\tclaude\t--continue",
            "proj\t/tmp\tgemini\t--resume latest",
            "proj\t/tmp\tcodex\t",
            "proj\t/tmp\t\tresume " + SESSION_ID,
            "proj\t/tmp\tcodex",
            "proj\t/tmp\tbash\t\textra",
            "proj\t/tmp\tcodex\tresume " + SESSION_ID + "; echo wrong",
            "proj\t/tmp\tbash\t\x00",
        ):
            with self.subTest(row=row):
                self.manifest.write_text(HEADER + row + "\n")
                result = self.run_helper("validate", self.manifest)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("manifest", result.stderr.lower())

    def test_validation_normalizes_exact_provider_command_and_preserves_empty_fields(self):
        self.manifest.write_text(HEADER + f"proj\t\t/usr/bin/codex resume {SESSION_ID}\t\n")
        result = self.run_helper("validate", self.manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, HEADER + f"proj\t\tcodex\tresume {SESSION_ID}\n")

    def test_duplicate_conversations_are_rejected_but_duplicate_names_are_valid(self):
        row = f"same\t/tmp\tcodex\tresume {SESSION_ID}\n"
        self.manifest.write_text(HEADER + row + row)
        result = self.run_helper("validate", self.manifest)
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate", result.stderr.lower())
        second = row.replace(SESSION_ID, "123e4567-e89b-42d3-a456-426614174001")
        self.manifest.write_text(HEADER + row + second)
        result = self.run_helper("validate", self.manifest)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_archive_failure_preserves_existing_snapshot(self):
        previous = HEADER + "previous\t/tmp\tbash\t\n"
        self.manifest.write_text(previous)
        candidate = self.root / "candidate.tsv"
        candidate.write_text(HEADER + "new\t/tmp\tbash\t\n")
        Path(str(self.manifest) + ".history").write_text("cannot create a directory here")
        result = self.run_helper("publish", candidate, self.manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.manifest.read_text(), previous)

    def test_merge_retains_closed_conversations_and_updates_live_metadata(self):
        old_id = "123e4567-e89b-42d3-a456-426614174001"
        previous = (
            HEADER
            + f"closed\t/tmp/old\tcodex\tresume {old_id}\n"
            + f"old-name\t/tmp\tcodex\tresume {SESSION_ID}\n"
        )
        self.manifest.write_text(previous)
        candidate = self.root / "candidate.tsv"
        candidate.write_text(HEADER + f"live\t/tmp/new\tcodex\tresume {SESSION_ID}\n")

        result = self.run_helper("publish", candidate, self.manifest, "--merge")

        self.assertEqual(result.returncode, 0, result.stderr)
        content = self.manifest.read_text()
        self.assertIn("closed\t/tmp/old", content)
        self.assertIn("live\t/tmp/new", content)
        self.assertNotIn("old-name", content)
        self.assertTrue(
            any(
                path.read_text() == previous
                for path in Path(str(self.manifest) + ".history").glob("*.tsv")
            )
        )

    def test_merge_preserves_repeated_generic_window_occurrences(self):
        row = "shell\t/tmp\tbash\t\n"
        self.manifest.write_text(HEADER + row + row)
        candidate = self.root / "candidate.tsv"
        candidate.write_text(HEADER + row)
        result = self.run_helper("publish", candidate, self.manifest, "--merge")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.manifest.read_text(), HEADER + row + row)

    def test_merge_archives_malformed_old_manifest_before_repair(self):
        previous = "malformed legacy manifest\n"
        self.manifest.write_text(previous)
        candidate = self.root / "candidate.tsv"
        candidate.write_text(HEADER + "shell\t/tmp\tbash\t\n")
        result = self.run_helper("publish", candidate, self.manifest, "--merge")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.manifest.read_bytes(), candidate.read_bytes())
        self.assertTrue(
            any(
                path.read_text() == previous
                for path in Path(str(self.manifest) + ".history").glob("*.tsv")
            )
        )

    @unittest.skipUnless(os.name == "posix", "requires flock")
    def test_manifest_operations_wait_for_same_manifest_lock(self):
        import fcntl

        marker = self.root / "ran"
        with Path(str(self.manifest) + ".lock").open("w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            child = subprocess.Popen(
                [
                    sys.executable,
                    str(HELPER),
                    "lock-run",
                    str(self.manifest),
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
                    str(marker),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    child.wait(timeout=0.2)
                self.assertFalse(marker.exists())
                fcntl.flock(handle, fcntl.LOCK_UN)
                stdout, stderr = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 0, stdout + stderr)
                self.assertTrue(marker.exists())
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate()
