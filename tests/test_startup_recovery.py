import importlib.util
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "startup_recovery", ROOT / "bin/codex-recover-startup.py"
)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
SESSION_ID = "123e4567-e89b-42d3-a456-426614174001"
ERROR = """ERROR: failed to initialize sqlite local
db at /home/test/.codex/state_5.sqlit
e: failed to open log DB at /home/test/.codex/logs_2.sqlite:
(code: 5) database is lo
cked
Pane is dead (status 1)
"""


class StartupRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fields = {
            "#{pane_dead}:#{pane_dead_status}": "1:1",
            "#{pane_dead_signal}": "",
            "#{@codexfarm_provider}": "codex",
            "#{@codexfarm_name}": "project",
            "#{@codexfarm_session_id}": SESSION_ID,
            "#{pane_start_command}": '"'
            + recovery.PREFIX
            + f"codex -c check_for_update_on_startup=false resume {SESSION_ID}"
            + '"',
        }
        self.output = ERROR
        self.calls = []

    def tmux(self, *args):
        self.calls.append(args)
        if args[0] == "list-panes":
            return "%5\n%5"
        if args[0] == "display-message":
            return self.fields[args[-1]]
        if args[0] == "capture-pane":
            return self.output
        if args[0] == "respawn-pane":
            return ""
        self.fail(args)

    def run_recovery(self, writer_status=0):
        with (
            patch.object(recovery, "tmux", side_effect=self.tmux),
            patch.object(recovery.shutil, "which", return_value="/current/bin/codex"),
            patch.object(
                recovery.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], writer_status),
            ),
        ):
            return recovery.recover("codexfarm")

    def respawns(self):
        return [call for call in self.calls if call[0] == "respawn-pane"]

    def test_retries_wrapped_sqlite_failure_once_with_exact_id_and_current_binary(self):
        self.assertEqual(self.run_recovery(), 0)
        self.assertEqual(
            self.respawns(),
            [
                (
                    "respawn-pane",
                    "-t",
                    "%5",
                    recovery.PREFIX
                    + f"/current/bin/codex -c check_for_update_on_startup=false resume {SESSION_ID}",
                )
            ],
        )
        self.assertNotIn("-k", self.respawns()[0])
        self.assertIn(("list-panes", "-s", "-t", "=codexfarm", "-F", "#{pane_id}"), self.calls)

    def test_live_panes_and_normal_exits_are_never_restarted(self):
        for state in ("0:", "1:0", "1:130"):
            with self.subTest(state=state):
                self.fields["#{pane_dead}:#{pane_dead_status}"] = state
                self.run_recovery()
                self.assertEqual(self.respawns(), [])

    def test_missing_exit_status_still_requires_the_exact_fatal_error(self):
        self.fields["#{pane_dead}:#{pane_dead_status}"] = "1:"
        self.assertEqual(self.run_recovery(), 0)
        self.assertEqual(len(self.respawns()), 1)
        self.calls.clear()
        self.output = "An unrelated failure"
        self.run_recovery()
        self.assertEqual(self.respawns(), [])

    def test_signal_terminated_panes_are_not_restarted(self):
        self.fields["#{pane_dead}:#{pane_dead_status}"] = "1:"
        self.fields["#{pane_dead_signal}"] = "15"
        self.run_recovery()
        self.assertEqual(self.respawns(), [])

    def test_unmanaged_or_other_provider_panes_are_untouched(self):
        for key, value in (("#{@codexfarm_name}", ""), ("#{@codexfarm_provider}", "claude")):
            with self.subTest(key=key):
                original = self.fields[key]
                self.fields[key] = value
                self.run_recovery()
                self.assertEqual(self.respawns(), [])
                self.fields[key] = original

    def test_active_conversation_writer_is_not_restarted(self):
        self.assertEqual(self.run_recovery(writer_status=3), 1)
        self.assertEqual(self.respawns(), [])

    def test_other_errors_and_old_lock_followed_by_new_fatal_error_are_ignored(self):
        for output in (
            "database is locked",
            "ERROR: authentication failed",
            ERROR + "ERROR: connection failed",
            ERROR + "Later unrelated process failure",
            ERROR.split("Pane is dead")[0] + "Later unrelated process failure",
        ):
            with self.subTest(output=output):
                self.output = output
                self.run_recovery()
                self.assertEqual(self.respawns(), [])

    def test_custom_commands_prompts_and_identity_mismatches_are_not_replayed(self):
        for command in (
            "codex resume --last",
            f"codex resume {SESSION_ID} 'do some work'",
            f"codex resume {SESSION_ID}; touch /tmp/unwanted",
            f"codex resume {SESSION_ID}$(touch /tmp/unwanted)",
            "codex resume 00000000-0000-0000-0000-000000000000",
            f"bash -c 'codex resume {SESSION_ID}'",
        ):
            with self.subTest(command=command):
                self.fields["#{pane_start_command}"] = command
                self.run_recovery()
                self.assertEqual(self.respawns(), [])

    def test_concurrent_recovery_cannot_kill_a_live_pane(self):
        original = self.tmux
        reads = 0

        def racing_tmux(*args):
            nonlocal reads
            if args[-1] == "#{pane_dead}:#{pane_dead_status}":
                reads += 1
                if reads > 1:
                    self.fields[args[-1]] = "0:"
            return original(*args)

        self.tmux = racing_tmux
        self.run_recovery()
        self.assertEqual(self.respawns(), [])

    def test_tmux_refusal_is_reported_without_force(self):
        original = self.tmux

        def refusing_tmux(*args):
            result = original(*args)
            if args[0] == "respawn-pane":
                raise subprocess.CalledProcessError(1, args)
            return result

        self.tmux = refusing_tmux
        self.assertEqual(self.run_recovery(), 1)
        self.assertNotIn("-k", self.respawns()[0])


@unittest.skipUnless(shutil.which("tmux"), "tmux is required")
class StartupRecoveryIntegrationTests(unittest.TestCase):
    def test_resume_recovers_on_private_tmux_server_and_preserves_cwd(self):
        real_tmux = shutil.which("tmux")
        # Keep the Unix socket below sockaddr_un's limit in guarded TMPDIRs.
        with tempfile.TemporaryDirectory(prefix="") as directory:
            root = Path(directory)
            socket = root / "s"
            env = {
                **os.environ,
                "PATH": f"{root}:{os.environ['PATH']}",
                "HOME": str(root),
                "CODEX_HOME": str(root / ".codex"),
                "TMUX": "",
            }
            (root / "project").mkdir()
            wrapper = root / "tmux"
            wrapper.write_text(
                '#!/bin/sh\nif [ "$1" = attach ]; then echo ATTACHED; exit 0; fi\nexec '
                + shlex.join([real_tmux, "-S", str(socket), "-f", "/dev/null"])
                + ' "$@"\n'
            )
            wrapper.chmod(0o700)
            codex = root / "codex"
            codex.write_text(
                '#!/bin/sh\nif [ ! -f "$HOME/attempted" ]; then\n'
                'touch "$HOME/attempted"\n'
                "echo 'ERROR: failed to initialize sqlite local db at state_5.sqlite: database is locked'\n"
                'exit 1\nfi\nprintf "RECOVERED:%s\\n" "$PWD"\nsleep 30\n'
            )
            codex.chmod(0o700)

            def run(*args):
                return subprocess.check_output([str(wrapper), *args], env=env, text=True).strip()

            try:
                run("new-session", "-d", "-x", "300", "-y", "30", "-s", "codexfarm", "sleep 30")
                pane = run(
                    "new-window",
                    "-P",
                    "-F",
                    "#{pane_id}",
                    "-t",
                    "codexfarm",
                    "-c",
                    str(root / "project"),
                    recovery.PREFIX + f"codex resume {SESSION_ID}",
                )
                for key, value in (
                    ("@codexfarm_provider", "codex"),
                    ("@codexfarm_name", "project"),
                    ("@codexfarm_session_id", SESSION_ID),
                ):
                    run("set-option", "-p", "-t", pane, key, value)
                deadline = time.monotonic() + 5
                while run("display-message", "-p", "-t", pane, "#{pane_dead}") != "1":
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.05)
                result = subprocess.run(
                    [str(ROOT / "bin/codex-resume")],
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("ATTACHED", result.stdout)
                self.assertIn(
                    "Retried Codex pane",
                    result.stderr,
                    run(
                        "display-message",
                        "-p",
                        "-t",
                        pane,
                        "#{pane_dead}:#{pane_dead_status} #{pane_start_command}",
                    )
                    + "\n"
                    + run("capture-pane", "-p", "-t", pane),
                )
                deadline = time.monotonic() + 5
                while "RECOVERED:" not in run("capture-pane", "-p", "-t", pane):
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.05)
                self.assertIn(
                    f"RECOVERED:{root / 'project'}", run("capture-pane", "-p", "-t", pane)
                )
                self.assertEqual(run("display-message", "-p", "-t", pane, "#{pane_dead}"), "0")
                self.assertEqual(
                    run("display-message", "-p", "-t", pane, "#{@codexfarm_session_id}"), SESSION_ID
                )
                again = subprocess.run(
                    [str(ROOT / "bin/codex-resume")],
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
                self.assertEqual(again.returncode, 0, again.stderr)
                self.assertNotIn("Retried", again.stderr)
            finally:
                subprocess.run([str(wrapper), "kill-server"], env=env, capture_output=True)


if __name__ == "__main__":
    unittest.main()
