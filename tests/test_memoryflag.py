import os
import shutil
import socket
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def make_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class MemoryFlagTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="memoryflag-test-"))
        self.bin_dir = self.tmpdir / "bin"
        self.bin_dir.mkdir()
        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.bin_dir}:{self.env.get('PATH', '')}"
        self.env["TMUX_TMPDIR"] = str(self.tmpdir / "tmux")
        self.env.pop("TMUX", None)

    def test_public_help_uses_wrapper_name(self) -> None:
        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-memoryflag", "--help"],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("Usage: codex-memoryflag", result.stdout)
        self.assertNotIn("add_high_memory_warning.sh", result.stdout)

    def test_implementation_avoids_associative_arrays_and_runuser(self) -> None:
        content = (REPO_ROOT / "bin" / "add_high_memory_warning.sh").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("declare -A", content)
        self.assertNotIn("runuser", content)

    def test_dry_run_preserves_run_prefix_when_marking_high_memory_window(self) -> None:
        socket_dir = Path(self.env["TMUX_TMPDIR"]) / f"tmux-{os.getuid()}"
        socket_dir.mkdir(parents=True)
        socket_path = socket_dir / "default"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.bind(str(socket_path))

        make_executable(
            self.bin_dir / "tmux",
            """#!/usr/bin/env bash
if [ "$1" = "-S" ]; then
  shift 2
fi
case "$1" in
  list-panes)
    printf 'farm\\t@1\\t1\\t*RUN* project\\t100\\n'
    ;;
  *)
    exit 1
    ;;
esac
""",
        )
        make_executable(
            self.bin_dir / "ps",
            """#!/usr/bin/env bash
printf '100 1 300000\\n'
""",
        )
        for command in ["awk", "basename", "cat", "dirname", "grep", "id", "mktemp", "sed", "stat", "tr"]:
            system_path = shutil.which(command)
            if not system_path:
                raise RuntimeError(f"Missing required test command: {command}")
            (self.bin_dir / command).symlink_to(system_path)

        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-memoryflag", "--dry-run"],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("*RUN* project -> *200+MB** *RUN* project", result.stdout)
