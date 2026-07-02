import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def make_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class StatusScriptTests(unittest.TestCase):
    def test_activity_shows_window_metadata_and_recent_pane_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="status-script-test-") as td:
            tmpdir = Path(td)
            tmux_stub = tmpdir / "tmux"
            make_executable(
                tmux_stub,
                """#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  has-session)
    [ "$3" = "work" ]
    ;;
  list-windows)
    printf '@1\\t1\\tclaude-loop\\tRUN\\t\\n'
    ;;
  list-panes)
    printf '%%1\\t/tmp/project\\tpython3\\tpython3 /repo/bin/codex-looper.py --agent claude\\n'
    ;;
  capture-pane)
    printf '%s\\n' '===== loop 3 / session cleanup-loop-0003 ====='
    printf '%s\\n' '--- prompt 2/3 ---'
    printf '%s\\n' '$ claude -p --output-format stream-json --verbose ...'
    ;;
  *)
    echo "unexpected tmux call: $*" >&2
    exit 99
    ;;
esac
""",
            )
            env = os.environ.copy()
            env["PATH"] = f"{tmpdir}:{env.get('PATH', '')}"
            env["CODEX_STATUS_ACTIVITY_LINES"] = "3"

            result = subprocess.run(
                [REPO_ROOT / "bin" / "codex-status", "--session", "work", "activity"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("=== Agent Activity ===", result.stdout)
        self.assertIn("1: [RUN] claude-loop (/tmp/project)", result.stdout)
        self.assertIn(
            "command: python3 /repo/bin/codex-looper.py --agent claude",
            result.stdout,
        )
        self.assertIn("recent:", result.stdout)
        self.assertIn("===== loop 3 / session cleanup-loop-0003 =====", result.stdout)
        self.assertIn("--- prompt 2/3 ---", result.stdout)

    def test_loopers_reads_durable_looper_state_without_tmux(self) -> None:
        with tempfile.TemporaryDirectory(prefix="status-script-test-") as td:
            project = Path(td)
            run_dir = project / ".agent-looper" / "runs" / "20260625T000000Z__state-smoke__abc123"
            run_dir.mkdir(parents=True)
            state = {
                "schema_version": 1,
                "status": "stopped",
                "label": "state-smoke",
                "agent_name": "generic",
                "agent_kind": "generic",
                "run_dir": str(run_dir),
                "current_loop": 2,
                "current_prompt_index": 1,
                "current_session_id": "session-abc",
                "updated_at": "2026-06-25T00:00:05Z",
                "stop_reason": "completion marker",
                "last_log": str(run_dir / "loop-0002__prompt-001.log"),
                "exit_code": 0,
            }
            (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
            bin_dir = project / "bin"
            bin_dir.mkdir()
            (bin_dir / "python3").symlink_to(sys.executable)
            env = os.environ.copy()
            env["PATH"] = str(bin_dir)

            result = subprocess.run(
                ["/bin/bash", str(REPO_ROOT / "bin" / "codex-status"), "loopers"],
                cwd=project,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("=== Looper Runs ===", result.stdout)
        self.assertIn("state-smoke: [stopped] generic/generic", result.stdout)
        self.assertIn("loop=2 prompt=1", result.stdout)
        self.assertIn("session=session-abc", result.stdout)
        self.assertIn("stop: completion marker", result.stdout)
        self.assertIn("loop-0002__prompt-001.log", result.stdout)

    def test_loopers_marks_and_repairs_stale_running_state(self) -> None:
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead_pid = dead.pid
        dead.wait(timeout=5)

        with tempfile.TemporaryDirectory(prefix="status-script-test-") as td:
            project = Path(td)
            run_dir = project / ".agent-looper" / "runs" / "20260702T000000Z__stale-smoke__abc123"
            run_dir.mkdir(parents=True)
            state = {
                "schema_version": 1,
                "status": "running",
                "pid": dead_pid,
                "label": "stale-smoke",
                "agent_name": "generic",
                "agent_kind": "generic",
                "run_dir": str(run_dir),
                "current_loop": 12,
                "current_prompt_index": 1,
                "updated_at": "2026-07-02T00:00:05Z",
            }
            state_path = run_dir / "state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            bin_dir = project / "bin"
            bin_dir.mkdir()
            (bin_dir / "python3").symlink_to(sys.executable)
            env = os.environ.copy()
            env["PATH"] = str(bin_dir)

            stale_result = subprocess.run(
                ["/bin/bash", str(REPO_ROOT / "bin" / "codex-status"), "loopers"],
                cwd=project,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(stale_result.returncode, 0, stale_result.stderr)
            self.assertIn("stale-smoke: [stale] generic/generic", stale_result.stdout)
            self.assertIn(f"stale: supervisor process {dead_pid} is no longer running", stale_result.stdout)
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["status"], "running")
            self.assertFalse((run_dir / "events.jsonl").exists())

            repair_result = subprocess.run(
                [
                    "/bin/bash",
                    str(REPO_ROOT / "bin" / "codex-status"),
                    "loopers",
                    "--repair-stale-loopers",
                ],
                cwd=project,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(repair_result.returncode, 0, repair_result.stderr)
            self.assertIn("stale-smoke: [stopped] generic/generic", repair_result.stdout)
            self.assertIn(f"repaired: supervisor process {dead_pid} is no longer running", repair_result.stdout)
            self.assertIn("stop: external termination", repair_result.stdout)
            repaired = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(repaired["status"], "stopped")
            self.assertEqual(repaired["last_event"], "run_stopped")
            events = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(events), 1)
            self.assertEqual(json.loads(events[0])["event"], "run_stopped")

    def test_loopers_imports_package_from_installed_bin_layout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="status-script-test-") as td:
            project = Path(td) / "project"
            project.mkdir()
            run_dir = project / ".agent-looper" / "runs" / "20260702T010000Z__installed__abc123"
            run_dir.mkdir(parents=True)
            (run_dir / "state.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "stopped",
                        "label": "installed",
                        "agent_name": "generic",
                        "agent_kind": "generic",
                        "run_dir": str(run_dir),
                        "updated_at": "2026-07-02T01:00:05Z",
                    }
                ),
                encoding="utf-8",
            )

            install_bin = Path(td) / "home" / "bin"
            install_bin.mkdir(parents=True)
            installed_status = install_bin / "codex-status"
            shutil.copy2(REPO_ROOT / "bin" / "codex-status", installed_status)
            shutil.copytree(REPO_ROOT / "codex_looper", install_bin / "codex_looper")
            (install_bin / "python3").symlink_to(sys.executable)
            env = os.environ.copy()
            env["PATH"] = str(install_bin)

            result = subprocess.run(
                ["/bin/bash", str(installed_status), "loopers"],
                cwd=project,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("installed: [stopped] generic/generic", result.stdout)


if __name__ == "__main__":
    unittest.main()
