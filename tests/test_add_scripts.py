import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def make_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class AddScriptsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="add-script-test-"))
        self.tmux_log = self.tmpdir / "tmux.log"

        tmux_stub = """#!/usr/bin/env bash
set -euo pipefail
log="${TMUX_LOG}"
echo "$*" >> "$log"
case "$1" in
  has-session)
    # trigger new-session path
    exit 1
    ;;
  new-window)
    echo "1"
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
"""
        make_executable(self.tmpdir / "tmux", tmux_stub)
        make_executable(self.tmpdir / "codex", "#!/usr/bin/env bash\nexit 0\n")
        make_executable(self.tmpdir / "claude", "#!/usr/bin/env bash\nexit 0\n")

        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.tmpdir}:{self.env.get('PATH', '')}"
        self.env["TMUX_LOG"] = str(self.tmux_log)
        self.env["XDG_STATE_HOME"] = str(self.tmpdir / "state")
        self.env["XDG_CONFIG_HOME"] = str(self.tmpdir / "config")
        self.env["HOME"] = str(self.tmpdir)

    def read_tmux_commands(self) -> list[list[str]]:
        lines = self.tmux_log.read_text(encoding="utf-8").splitlines()
        return [line.split() for line in lines]

    def test_claude_add_passthrough_and_shorthand(self):
        target_dir = self.tmpdir / "proj"
        target_dir.mkdir(parents=True, exist_ok=True)

        subprocess.run(
            [REPO_ROOT / "bin" / "claude-add", "-d", "-dsp", str(target_dir)],
            check=True,
            env=self.env,
        )

        commands = self.read_tmux_commands()
        new_window = [cmd for cmd in commands if cmd and cmd[0] == "new-window"]
        self.assertEqual(len(new_window), 1, f"Unexpected tmux commands: {commands}")
        self.assertIn("-t", new_window[0])
        self.assertIn("codexfarm", new_window[0], "Session should default to codexfarm")
        self.assertIn(
            "claude --dangerously-skip-permissions",
            " ".join(new_window[0]),
            "claude command should receive expanded -dsp flag",
        )

    def test_codex_add_passes_unknown_flags_to_tool(self):
        target_dir = self.tmpdir / "proj2"
        target_dir.mkdir(parents=True, exist_ok=True)

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-add", "-d", "--some-flag", str(target_dir)],
            check=True,
            env=self.env,
        )

        commands = self.read_tmux_commands()
        new_window = [cmd for cmd in commands if cmd and cmd[0] == "new-window"]
        self.assertEqual(len(new_window), 1)
        self.assertIn("codex --some-flag", " ".join(new_window[0]))

    def test_autoservice_choice_persist_no(self):
        target_dir = self.tmpdir / "proj3"
        target_dir.mkdir(parents=True, exist_ok=True)

        env = self.env.copy()
        env["CODEX_AUTOSERVICE_CHOICE"] = "no"
        env["SYSTEMCTL_BIN"] = str(self.tmpdir / "systemctl")
        make_executable(self.tmpdir / "systemctl", "#!/usr/bin/env bash\nexit 0\n")

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-add", "-d", str(target_dir)],
            check=True,
            env=env,
        )

        choice_file = Path(env["XDG_STATE_HOME"]) / "codexfarm" / "autoservice_choice"
        self.assertTrue(choice_file.exists())
        self.assertEqual(choice_file.read_text().strip(), "no")

    def test_autoservice_install_creates_units(self):
        target_dir = self.tmpdir / "proj4"
        target_dir.mkdir(parents=True, exist_ok=True)

        systemctl_log = self.tmpdir / "systemctl.log"
        systemctl_stub = f"""#!/usr/bin/env bash
echo "$*" >> "{systemctl_log}"
exit 0
"""
        make_executable(self.tmpdir / "systemctl", systemctl_stub)

        env = self.env.copy()
        env["CODEX_AUTOSERVICE_CHOICE"] = "yes"
        env["SYSTEMCTL_BIN"] = str(self.tmpdir / "systemctl")

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-add", "-d", str(target_dir)],
            check=True,
            env=env,
        )

        systemd_dir = Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user"
        autosave = systemd_dir / "codex-autosave.service"
        autosave_timer = systemd_dir / "codex-autosave.timer"
        autorestore = systemd_dir / "codex-autorestore.service"
        self.assertTrue(autosave.exists(), "autosave unit missing")
        self.assertTrue(autosave_timer.exists(), "autosave timer missing")
        self.assertTrue(autorestore.exists(), "autorestore unit missing")
        autosave_content = autosave.read_text()
        autosave_timer_content = autosave_timer.read_text()
        autorestore_content = autorestore.read_text()
        self.assertIn("codex-save", autosave_content)
        self.assertIn("ExecStart=", autosave_content)
        self.assertIn("OnCalendar=hourly", autosave_timer_content)
        self.assertIn("Unit=codex-autosave.service", autosave_timer_content)
        self.assertIn("codex-restore -a", autorestore_content)

        choice_file = Path(env["XDG_STATE_HOME"]) / "codexfarm" / "autoservice_choice"
        self.assertEqual(choice_file.read_text().strip(), "yes")

        systemctl_calls = systemctl_log.read_text().splitlines()
        self.assertTrue(any("--user" in line for line in systemctl_calls))
        self.assertTrue(
            any("codex-autosave.timer" in line for line in systemctl_calls),
            f"Expected timer-related systemctl calls, got: {systemctl_calls}",
        )


if __name__ == "__main__":
    unittest.main()
