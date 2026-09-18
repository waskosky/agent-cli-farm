import json
import os
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
        temporary = tempfile.TemporaryDirectory(prefix="memoryflag-test-")
        self.addCleanup(temporary.cleanup)
        self.tmpdir = Path(temporary.name)
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
        content = (REPO_ROOT / "bin" / "add_high_memory_warning.sh").read_text(encoding="utf-8")

        self.assertNotIn("declare -A", content)
        self.assertNotIn("runuser", content)

    def prepare_tmux(self) -> None:
        socket_dir = Path(self.env["TMUX_TMPDIR"]) / f"tmux-{os.getuid()}"
        socket_dir.mkdir(parents=True)
        socket_path = socket_dir / "default"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.bind(str(socket_path))

        make_executable(
            self.bin_dir / "tmux",
            """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[3:] if sys.argv[1] == '-S' else sys.argv[1:]
with open(os.environ['FAKE_TMUX_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
if args[0] == 'list-panes':
    print(os.environ['FAKE_PANES'])
""",
        )
        make_executable(
            self.bin_dir / "ps",
            """#!/usr/bin/env bash
printf '%s\\n' "$FAKE_PROCESSES"
""",
        )
        self.env["FAKE_TMUX_LOG"] = str(self.tmpdir / "tmux.log")
        self.env["FAKE_PANES"] = "farm\t@1\t1\t*RUN* project\t100"
        self.env["FAKE_PROCESSES"] = "100 1 300000"

    def run_memoryflag(self, *args):
        Path(self.env["FAKE_TMUX_LOG"]).write_text("")
        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-memoryflag", *args],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [
            json.loads(line) for line in Path(self.env["FAKE_TMUX_LOG"]).read_text().splitlines()
        ]
        return result, calls

    def test_dry_run_shows_measured_usage_without_mutating_titles_or_options(self) -> None:
        self.prepare_tmux()
        result, calls = self.run_memoryflag("--dry-run")
        self.assertIn("*RUN* project -> *293.0MB** *RUN* project", result.stdout)
        self.assertTrue(all(call[0] == "list-panes" for call in calls))

    def test_threshold_gates_measured_labels_and_refresh_is_opted_in(self) -> None:
        self.prepare_tmux()
        for threshold, rss, expected in (
            ("200", 204799, "*RUN* project"),
            ("200", 204800, "*200.0MB** *RUN* project"),
            ("1G", 1536000, "*1500.0MB** *RUN* project"),
        ):
            with self.subTest(threshold=threshold, rss=rss):
                self.env["FAKE_PROCESSES"] = f"100 1 {rss}"
                self.env["FAKE_PANES"] = "farm\t@1\t1\t*200+MB** *RUN* project\t100"
                _, calls = self.run_memoryflag(threshold)
                self.assertIn(["rename-window", "-t", "@1", expected], calls)
                self.assertIn(
                    [
                        "set-window-option",
                        "-t",
                        "@1",
                        "@codexfarm_memory_threshold_mib",
                        "1024" if threshold == "1G" else threshold,
                    ],
                    calls,
                )

    def test_refreshes_and_clears_new_labels_without_stacking_markers(self) -> None:
        self.prepare_tmux()
        self.env["FAKE_PANES"] = "farm\t@1\t1\t*READY* *512.1MB** *RUN* project\t100"
        _, calls = self.run_memoryflag()
        self.assertIn(["rename-window", "-t", "@1", "*293.0MB** *RUN* project"], calls)
        self.env["FAKE_PROCESSES"] = "100 1 102400"
        _, calls = self.run_memoryflag()
        self.assertIn(["rename-window", "-t", "@1", "*RUN* project"], calls)

    def test_unchanged_window_still_registers_threshold_for_future_growth(self) -> None:
        self.prepare_tmux()
        self.env["FAKE_PROCESSES"] = "100 1 102400"
        _, calls = self.run_memoryflag("500")
        self.assertFalse(any(call[0] == "rename-window" for call in calls))
        self.assertIn(
            ["set-window-option", "-t", "@1", "@codexfarm_memory_threshold_mib", "500"], calls
        )

    def test_counts_descendants_once_across_panes_and_linked_windows(self) -> None:
        self.prepare_tmux()
        self.env["FAKE_PROCESSES"] = "100 1 102400\n101 100 153600\n102 101 51200\n200 1 999999"
        self.env["FAKE_PANES"] = (
            "farm\t@1\t1\t*RUN* project\t100\n"
            "farm\t@1\t1\t*RUN* project\t101\n"
            "board\t@1\t1\t*RUN* project\t100"
        )
        _, calls = self.run_memoryflag()
        self.assertEqual(
            [call for call in calls if call[0] == "rename-window"],
            [["rename-window", "-t", "@1", "*300.0MB** *RUN* project"]],
        )
