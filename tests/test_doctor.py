from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCTOR_BIN = REPO_ROOT / "bin" / "codex-doctor"
SESSION_ID = "019e1659-3a2f-7a40-95cf-5ac9dd7fe5d4"


def make_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="doctor-test-")
        self.root = Path(self.temp_dir.name)
        self.home = self.root / "home"
        self.source = self.root / "source"
        self.source_bin = self.source / "bin"
        self.install_bin = self.home / "bin"
        self.fake_bin = self.root / "fake-bin"
        self.manifest = self.root / "manifest.tsv"
        for directory in (self.home, self.source_bin, self.install_bin, self.fake_bin):
            directory.mkdir(parents=True, exist_ok=True)

        (self.source / "setup.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        for name in ("codex-add", "codex-save", "codex-restore"):
            make_executable(self.source_bin / name, f"#!/usr/bin/env bash\n# {name}\n")
            shutil.copy2(self.source_bin / name, self.install_bin / name)

        make_executable(
            self.fake_bin / "tmux",
            """#!/usr/bin/env bash
case "${1:-}" in
  has-session) exit 0 ;;
  list-windows) printf 'home\\nproject\\n' ;;
esac
""",
        )
        make_executable(self.fake_bin / "systemctl", "#!/usr/bin/env bash\nexit 1\n")

        self.manifest.write_text(
            f"name\tdir\tcmd\targs\nproject\t/tmp/project\tcodex\tresume {SESSION_ID}\n",
            encoding="utf-8",
        )
        self.manifest.chmod(0o600)

        self.env = os.environ.copy()
        self.env["HOME"] = str(self.home)
        self.env["XDG_CONFIG_HOME"] = str(self.root / "config")
        self.env["XDG_STATE_HOME"] = str(self.root / "state")
        self.env["CODEXFARM_SOURCE_DIR"] = str(self.source)
        self.env["CODEXFARM_INSTALL_DIR"] = str(self.install_bin)
        self.env["PATH"] = f"{self.fake_bin}:{self.env.get('PATH', '')}"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_doctor(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [DOCTOR_BIN, "--session", "test", *arguments],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_healthy_install_and_exact_manifest(self) -> None:
        result = self.run_doctor(self.manifest)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("all 3 installed helpers match", result.stdout)
        self.assertIn("exact provider sessions: 1", result.stdout)
        self.assertIn("fallback: 0", result.stdout)
        self.assertIn("Doctor result: healthy", result.stdout)
        self.assertNotIn(SESSION_ID, result.stdout)

    def test_stale_installed_helper_is_actionable(self) -> None:
        (self.install_bin / "codex-save").write_text("stale\n", encoding="utf-8")

        result = self.run_doctor(self.manifest)

        self.assertEqual(result.returncode, 1)
        self.assertIn("1 of 3 installed helpers are missing or stale: codex-save", result.stdout)

    def test_installed_copy_uses_setup_source_marker(self) -> None:
        installed_doctor = self.install_bin / "codex-doctor"
        shutil.copy2(DOCTOR_BIN, installed_doctor)
        source_marker = Path(self.env["XDG_STATE_HOME"]) / "codexfarm" / "install-source"
        source_marker.parent.mkdir(parents=True)
        source_marker.write_text(f"{self.source}\n", encoding="utf-8")
        env = self.env.copy()
        env.pop("CODEXFARM_SOURCE_DIR")

        result = subprocess.run(
            [installed_doctor, "--session", "test", self.manifest],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("all 3 installed helpers match", result.stdout)

    def test_blank_and_fallback_manifest_entries_fail_without_printing_ids(self) -> None:
        self.manifest.write_text(
            "name\tdir\tcmd\targs\n"
            "blank\t/tmp/blank\t\t\n"
            "fallback\t/tmp/fallback\tcodex\tresume --last\n",
            encoding="utf-8",
        )
        self.manifest.chmod(0o600)

        result = self.run_doctor(self.manifest)

        self.assertEqual(result.returncode, 1)
        self.assertIn("fallback: 1", result.stdout)
        self.assertIn("blank commands: 1", result.stdout)
        self.assertIn("save again before restoring", result.stdout)

    def test_malformed_header_fails_cleanly(self) -> None:
        self.manifest.write_text("not-a-manifest\n", encoding="utf-8")

        result = self.run_doctor(self.manifest)

        self.assertEqual(result.returncode, 1)
        self.assertIn("malformed manifest header", result.stdout)

    def test_option_like_resume_value_is_not_counted_as_an_exact_session(self) -> None:
        self.manifest.write_text(
            "name\tdir\tcmd\targs\nproject\t/tmp/project\tcodex\tresume --dangerous\n",
            encoding="utf-8",
        )
        self.manifest.chmod(0o600)

        result = self.run_doctor(self.manifest)

        self.assertEqual(result.returncode, 1)
        self.assertIn("exact provider sessions: 0", result.stdout)
        self.assertIn("provider command(s) with invalid resume arguments", result.stdout)

    def test_non_uuid_resume_value_is_not_counted_as_an_exact_session(self) -> None:
        self.manifest.write_text(
            "name\tdir\tcmd\targs\nproject\t/tmp/project\tcodex\tresume not-a-uuid\n",
            encoding="utf-8",
        )
        self.manifest.chmod(0o600)

        result = self.run_doctor(self.manifest)

        self.assertEqual(result.returncode, 1)
        self.assertIn("exact provider sessions: 0", result.stdout)
        self.assertIn("provider command(s) with invalid resume arguments", result.stdout)

    def test_duplicate_logical_names_are_reported_without_printing_ids(self) -> None:
        second_id = "123e4567-e89b-42d3-a456-426614174001"
        self.manifest.write_text(
            "name\tdir\tcmd\targs\n"
            f"project\t/tmp/project\tcodex\tresume {SESSION_ID}\n"
            f"project\t/tmp/other\tcodex\tresume {second_id}\n",
            encoding="utf-8",
        )
        self.manifest.chmod(0o600)

        result = self.run_doctor(self.manifest)

        self.assertEqual(result.returncode, 1)
        self.assertIn("duplicate names: 1", result.stdout)
        self.assertIn("duplicate logical name occurrence", result.stdout)
        self.assertNotIn(SESSION_ID, result.stdout)
        self.assertNotIn(second_id, result.stdout)

    def test_rejects_duplicate_session_arguments(self) -> None:
        result = subprocess.run(
            [DOCTOR_BIN, "--session", "one", "--session", "two", self.manifest],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Session specified multiple times", result.stderr)


if __name__ == "__main__":
    unittest.main()
