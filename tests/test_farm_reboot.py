from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REBOOT_BIN = REPO_ROOT / "bin" / "codex-farm-reboot"


def make_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class FarmRebootTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="farm-reboot-test-")
        self.root = Path(self.temp_dir.name)
        self.log = self.root / "commands.log"
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()

        tmux_stub = r"""#!/usr/bin/env bash
set -euo pipefail
printf 'tmux %s\n' "$*" >> "$REBOOT_LOG"
case "${1:-}" in
  has-session)
    target="${3:-}"
    case " ${TMUX_EXISTING_SESSIONS:-} " in
      *" $target "*) exit 0 ;;
      *) exit 1 ;;
    esac
    ;;
  display-message)
    printf '%s\n' "${TMUX_CURRENT_SESSION:-}"
    ;;
  kill-session)
    if [ "${3:-}" = "${TMUX_KILL_FAIL_TARGET:-}" ]; then
      exit 1
    fi
    ;;
  attach|switch-client)
    ;;
esac
"""
        helper_stub = r"""#!/usr/bin/env bash
set -euo pipefail
kind="${0##*/}"
kind="${kind%-helper}"
printf '%s tool=%s session=%s args=%s\n' \
  "$kind" "${CODEX_TOOL_NAME:-}" "${CODEX_SESSION:-}" "$*" >> "$REBOOT_LOG"
case "$kind" in
  save) exit "${SAVE_EXIT:-0}" ;;
  restore) exit "${RESTORE_EXIT:-0}" ;;
  board) exit "${BOARD_EXIT:-0}" ;;
esac
"""
        make_executable(self.bin_dir / "tmux", tmux_stub)
        for helper in ("save", "restore", "board"):
            make_executable(self.bin_dir / f"{helper}-helper", helper_stub)

        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.bin_dir}:{self.env.get('PATH', '')}"
        self.env["REBOOT_LOG"] = str(self.log)
        self.env["CODEX_SAVE_BIN"] = str(self.bin_dir / "save-helper")
        self.env["CODEX_RESTORE_BIN"] = str(self.bin_dir / "restore-helper")
        self.env["CODEX_BOARD_BIN"] = str(self.bin_dir / "board-helper")
        self.env.pop("TMUX", None)
        self.env.pop("CODEX_SESSION", None)
        self.env.pop("CLAUDE_SESSION", None)
        self.env.pop("GEMINI_SESSION", None)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_reboot(
        self, *arguments: str, executable: Path = REBOOT_BIN, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [executable, *arguments],
            env=env or self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def log_lines(self) -> list[str]:
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()

    def test_reboots_default_farm_and_relinks_existing_board(self) -> None:
        self.env["TMUX_EXISTING_SESSIONS"] = "codexfarm board"

        result = self.run_reboot("--detach")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.log_lines(),
            [
                "tmux has-session -t codexfarm",
                "tmux has-session -t board",
                "save tool=codex session=codexfarm args=",
                "tmux kill-session -t board",
                "tmux kill-session -t codexfarm",
                "restore tool=codex session=codexfarm args=",
                "board tool=codex session=codexfarm args=link codexfarm",
            ],
        )
        self.assertIn("Farm codexfarm reboot complete.", result.stdout)

    def test_reboots_named_farm_without_creating_absent_board(self) -> None:
        self.env["TMUX_EXISTING_SESSIONS"] = "work"

        result = self.run_reboot("work", "--detach")

        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.log_lines()
        self.assertIn("tmux has-session -t work", lines)
        self.assertIn("tmux has-session -t work-board", lines)
        self.assertIn("save tool=codex session=work args=", lines)
        self.assertIn("restore tool=codex session=work args=", lines)
        self.assertNotIn("tmux kill-session -t work-board", lines)
        self.assertFalse(any(line.startswith("board ") for line in lines))

    def test_attaches_after_restore_by_default(self) -> None:
        self.env["TMUX_EXISTING_SESSIONS"] = "codexfarm"

        result = self.run_reboot()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log_lines()[-1], "tmux attach -t codexfarm")

    def test_switches_client_when_called_from_another_tmux_session(self) -> None:
        env = self.env.copy()
        env["TMUX_EXISTING_SESSIONS"] = "codexfarm maintenance"
        env["TMUX_CURRENT_SESSION"] = "maintenance"
        env["TMUX"] = "/tmp/tmux-fake/default,1,0"

        result = self.run_reboot(env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log_lines()[-1], "tmux switch-client -t codexfarm")

    def test_save_failure_never_stops_session(self) -> None:
        self.env["TMUX_EXISTING_SESSIONS"] = "codexfarm board"
        self.env["SAVE_EXIT"] = "9"

        result = self.run_reboot("--detach")

        self.assertEqual(result.returncode, 1)
        self.assertIn("Save failed", result.stderr)
        lines = self.log_lines()
        self.assertFalse(any("kill-session" in line for line in lines))
        self.assertFalse(any(line.startswith("restore ") for line in lines))

    def test_restore_failure_reports_recovery_command(self) -> None:
        self.env["TMUX_EXISTING_SESSIONS"] = "codexfarm board"
        self.env["RESTORE_EXIT"] = "8"

        result = self.run_reboot("--detach")

        self.assertEqual(result.returncode, 1)
        self.assertIn("Restore failed", result.stderr)
        lines = self.log_lines()
        self.assertIn("tmux kill-session -t codexfarm", lines)
        self.assertFalse(any(line.startswith("board ") for line in lines))

    def test_failed_farm_stop_relinks_board_before_returning(self) -> None:
        self.env["TMUX_EXISTING_SESSIONS"] = "codexfarm board"
        self.env["TMUX_KILL_FAIL_TARGET"] = "codexfarm"

        result = self.run_reboot("--detach")

        self.assertEqual(result.returncode, 1)
        self.assertIn("Unable to stop farm", result.stderr)
        self.assertIn("board tool=codex session=codexfarm args=link codexfarm", self.log_lines())

    def test_refuses_to_reboot_from_target_or_board_before_save(self) -> None:
        for current_session in ("codexfarm", "board"):
            with self.subTest(current_session=current_session):
                self.log.unlink(missing_ok=True)
                env = self.env.copy()
                env["TMUX_EXISTING_SESSIONS"] = "codexfarm board"
                env["TMUX_CURRENT_SESSION"] = current_session
                env["TMUX"] = "/tmp/tmux-fake/default,1,0"

                result = self.run_reboot("--detach", env=env)

                self.assertEqual(result.returncode, 2)
                self.assertIn("Cannot reboot", result.stderr)
                lines = self.log_lines()
                self.assertFalse(any(line.startswith("save ") for line in lines))
                self.assertFalse(any("kill-session" in line for line in lines))

    def test_provider_wrappers_select_matching_tool(self) -> None:
        self.env["TMUX_EXISTING_SESSIONS"] = "codexfarm"
        for tool in ("claude", "gemini"):
            with self.subTest(tool=tool):
                self.log.unlink(missing_ok=True)
                result = self.run_reboot(
                    "--detach", executable=REPO_ROOT / "bin" / f"{tool}-farm-reboot"
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                lines = self.log_lines()
                self.assertIn(f"save tool={tool} session=codexfarm args=", lines)
                self.assertIn(f"restore tool={tool} session=codexfarm args=", lines)

    def test_provider_session_environment_is_honored(self) -> None:
        self.env["TMUX_EXISTING_SESSIONS"] = "research"
        self.env["CLAUDE_SESSION"] = "research"

        result = self.run_reboot("--detach", executable=REPO_ROOT / "bin" / "claude-farm-reboot")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("save tool=claude session=research args=", self.log_lines())

    def test_missing_farm_and_duplicate_session_fail_cleanly(self) -> None:
        missing = self.run_reboot("--detach")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("is not running", missing.stderr)
        self.assertFalse(any(line.startswith("save ") for line in self.log_lines()))

        duplicate = self.run_reboot("--session", "work", "other")
        self.assertEqual(duplicate.returncode, 2)
        self.assertIn("Session specified multiple times", duplicate.stderr)

    def test_wrapper_help_uses_provider_command_name(self) -> None:
        result = self.run_reboot("--help", executable=REPO_ROOT / "bin" / "gemini-farm-reboot")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage: gemini-farm-reboot", result.stdout)


if __name__ == "__main__":
    unittest.main()
