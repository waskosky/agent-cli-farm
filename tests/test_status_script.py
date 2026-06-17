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


if __name__ == "__main__":
    unittest.main()
