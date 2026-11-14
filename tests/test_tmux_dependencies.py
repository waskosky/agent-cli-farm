import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class TmuxDependencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="tmux-dependencies-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tools = self.root / "tools with spaces"
        self.tools.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        for command in ("bash", "basename", "dirname", "tr", "cat", "awk", "python3"):
            (self.tools / command).symlink_to(shutil.which(command))
        self.env = {
            "PATH": str(self.tools),
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / "config"),
            "XDG_STATE_HOME": str(self.home / "state"),
            "XDG_DATA_HOME": str(self.home / "data"),
            "LC_ALL": "C",
        }
        self.package_log = self.root / "package-manager-called"
        self.env["PACKAGE_LOG"] = str(self.package_log)

    def fake_manager(self, name):
        path = self.tools / name
        path.write_text('#!/bin/bash\nprintf called >> "$PACKAGE_LOG"\nexit 98\n')
        path.chmod(0o755)
        return path

    def run_script(self, name, *args):
        return subprocess.run(
            ["/bin/bash", str(REPO_ROOT / "bin" / name), *args],
            cwd=self.home,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=10,
        )

    def test_hints_detect_package_managers_without_executing_them(self):
        self.fake_manager("sudo")
        for manager, instruction in (
            ("apt-get", "sudo apt-get install tmux"),
            ("apt", "sudo apt install tmux"),
            ("brew", "brew install tmux"),
            ("dnf", "sudo dnf install tmux"),
            ("yum", "sudo yum install tmux"),
            ("pacman", "sudo pacman -S tmux"),
            ("zypper", "sudo zypper install tmux"),
            ("apk", "sudo apk add tmux"),
            ("pkg", "sudo pkg install tmux"),
        ):
            with self.subTest(manager=manager):
                path = self.fake_manager(manager)
                try:
                    result = self.run_script("codex-resume")
                    self.assertEqual(result.returncode, 127, result.stderr)
                    self.assertIn(instruction, result.stderr)
                    self.assertFalse(self.package_log.exists())
                finally:
                    path.unlink()

    def test_unknown_package_manager_has_manual_install_and_path_guidance(self):
        result = self.run_script("codex-resume")
        self.assertEqual(result.returncode, 127, result.stderr)
        self.assertIn("system package manager", result.stderr)
        self.assertIn("inside WSL on Windows", result.stderr)
        self.assertIn("add its directory to PATH", result.stderr)
        self.assertIn("tmux -V", result.stderr)

    def test_farm_commands_fail_before_state_changes_with_install_instructions(self):
        self.fake_manager("apt-get")
        commands = (
            ("codex-add", "-d"),
            ("codex-board",),
            ("codex-resume",),
            ("codex-save",),
            ("codex-save", "--all-registered"),
            ("codex-restore",),
            ("codex-restore", "--all-registered"),
            ("codex-farm-reboot", "--detach"),
            ("codex-status", "sessions"),
            ("codex-status", "windows"),
            ("codex-status", "activity"),
            ("claude-add", "-d"),
            ("gemini-add", "-d"),
        )
        for command in commands:
            with self.subTest(command=command):
                result = self.run_script(*command)
                self.assertEqual(result.returncode, 127, result.stdout + result.stderr)
                self.assertIn("tmux not found on PATH", result.stderr)
                self.assertIn("sudo apt-get install tmux", result.stderr)
                self.assertFalse(self.package_log.exists())
                self.assertEqual(list(self.home.iterdir()), [])

    def test_help_and_log_view_remain_available_without_tmux(self):
        for name in (
            "codex-add",
            "codex-board",
            "codex-resume",
            "codex-save",
            "codex-restore",
            "codex-farm-reboot",
            "codex-status",
            "codex-doctor",
        ):
            with self.subTest(command=name):
                result = self.run_script(name, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Usage:", result.stdout)
        result = self.run_script("codex-status", "logs")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("tmux not found", result.stderr)
        result = self.run_script("codex-restore", "--list-snapshots")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_doctor_reports_install_guidance_without_changing_private_state(self):
        self.fake_manager("apt-get")
        result = self.run_script("codex-doctor", "--check")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("tmux is not available on PATH", result.stdout)
        self.assertIn("sudo apt-get install tmux", result.stdout)
        self.assertFalse(self.package_log.exists())
        self.assertEqual(list(self.home.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
