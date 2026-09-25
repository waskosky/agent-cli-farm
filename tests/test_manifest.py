import os
import subprocess
import sys
import tempfile
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
