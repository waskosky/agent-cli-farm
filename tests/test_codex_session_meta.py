from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - the farm currently targets tmux hosts
    fcntl = None


REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "bin" / "codex-session-meta.py"
ROOT_ID = "123e4567-e89b-42d3-a456-426614174000"
FORK_ID = "123e4567-e89b-42d3-a456-426614174001"
CHILD_ID = "123e4567-e89b-42d3-a456-426614174002"


class CodexSessionMetaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="codex-session-meta-test-")
        self.codex_home = Path(self.temp_dir.name) / ".codex"
        self.sessions_dir = self.codex_home / "sessions" / "2026" / "09" / "01"
        self.sessions_dir.mkdir(parents=True)
        self.env = os.environ.copy()
        self.env["CODEX_HOME"] = str(self.codex_home)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_rollout(self, thread_id: str, payload: dict[str, object]) -> Path:
        path = self.sessions_dir / f"rollout-2026-09-01T00-00-00-{thread_id}.jsonl"
        path.write_text(
            json.dumps({"type": "session_meta", "payload": payload})
            + "\n"
            + '{"type":"response_item","payload":{"private":"not metadata"}}\n',
            encoding="utf-8",
        )
        return path

    def run_helper(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, HELPER, *arguments],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_resolve_path_keeps_root_thread_id(self) -> None:
        path = self.write_rollout(
            ROOT_ID,
            {"id": ROOT_ID, "session_id": ROOT_ID, "source": "cli"},
        )

        result = self.run_helper("resolve-path", str(path))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, ROOT_ID)

    def test_resolve_path_keeps_independent_fork_id(self) -> None:
        path = self.write_rollout(
            FORK_ID,
            {
                "id": FORK_ID,
                "session_id": FORK_ID,
                "parent_thread_id": ROOT_ID,
                "source": "cli",
                "forked_from_id": ROOT_ID,
            },
        )

        result = self.run_helper("resolve-path", str(path))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, FORK_ID)

    def test_resolve_path_maps_multi_agent_child_to_session_root(self) -> None:
        path = self.write_rollout(
            CHILD_ID,
            {
                "id": CHILD_ID,
                "session_id": ROOT_ID,
                "parent_thread_id": ROOT_ID,
                "forked_from_id": ROOT_ID,
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": ROOT_ID,
                            "depth": 1,
                            "agent_path": "/root/reviewer",
                        }
                    }
                },
            },
        )

        result = self.run_helper("resolve-path", str(path))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, ROOT_ID)

    def test_resolve_id_discovers_and_repairs_existing_child_rollout(self) -> None:
        self.write_rollout(
            CHILD_ID,
            {
                "id": CHILD_ID,
                "session_id": ROOT_ID,
                "parent_thread_id": ROOT_ID,
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": ROOT_ID}}},
            },
        )

        result = self.run_helper("resolve-id", CHILD_ID)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, ROOT_ID)

    @unittest.skipIf(fcntl is None, "advisory file locks are unavailable")
    def test_wait_writable_rejects_active_lock_and_accepts_stale_lock_file(self) -> None:
        lock_dir = self.codex_home / "thread-writer-locks"
        lock_dir.mkdir(parents=True)
        lock_path = lock_dir / f"{ROOT_ID}.lock"
        with lock_path.open("a+b") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            blocked = self.run_helper("wait-writable", ROOT_ID, "0")

            self.assertEqual(blocked.returncode, 3)
            self.assertIn("active writer", blocked.stderr.lower())
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

        available = self.run_helper("wait-writable", ROOT_ID, "0")
        self.assertEqual(available.returncode, 0, available.stderr)

    def test_wait_writable_accepts_configurable_waits_above_five_minutes(self) -> None:
        result = self.run_helper("wait-writable", ROOT_ID, "301")

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_wait_writable_rejects_non_finite_wait(self) -> None:
        result = self.run_helper("wait-writable", ROOT_ID, "inf")

        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid", result.stderr.lower())

    def test_resolve_pid_uses_held_writer_lock_without_open_rollout(self) -> None:
        proc = Path(self.temp_dir.name) / "proc" / "101"
        (proc / "fd").mkdir(parents=True)
        (proc / "fdinfo").mkdir()
        locks = self.codex_home / "thread-writer-locks"
        locks.mkdir()
        lock = locks / f"{ROOT_ID}.lock"
        lock.touch()
        (proc / "fd" / "42").symlink_to(lock)
        (proc / "fdinfo" / "42").write_text("lock:\t1: FLOCK ADVISORY WRITE 101 00:01:1 0 EOF\n")
        self.env["CODEX_PROC_ROOT"] = str(proc.parent)

        result = self.run_helper("resolve-pid", "101")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, ROOT_ID)
        (proc / "fdinfo" / "42").write_text("pos:\t0\n")
        self.assertEqual(self.run_helper("resolve-pid", "101").returncode, 1)

    def test_resolve_pid_rejects_ambiguous_held_locks(self) -> None:
        proc = Path(self.temp_dir.name) / "proc" / "101"
        (proc / "fd").mkdir(parents=True)
        (proc / "fdinfo").mkdir()
        locks = self.codex_home / "thread-writer-locks"
        locks.mkdir()
        for number, thread_id in enumerate([ROOT_ID, FORK_ID]):
            lock = locks / f"{thread_id}.lock"
            lock.touch()
            (proc / "fd" / str(number)).symlink_to(lock)
            (proc / "fdinfo" / str(number)).write_text("lock:\t1: FLOCK ADVISORY WRITE 101\n")
        self.env["CODEX_PROC_ROOT"] = str(proc.parent)

        result = self.run_helper("resolve-pid", "101")

        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
