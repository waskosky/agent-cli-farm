from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "doctor_repair", REPO_ROOT / "bin/codex-doctor-repair.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DoctorRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {
            "HOME": str(self.root / "home"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_CONFIG_DIRS": str(self.root / "global-config"),
            "XDG_DATA_DIRS": str(self.root / "global-data"),
        }
        self.environment = patch.dict(os.environ, self.env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.repair = module.Repair(None, self.root / "bin", "test", self.root / "manifest.tsv")
        self.repair.state.mkdir(parents=True)
        self.repair.install.mkdir()
        self.calls = []
        self.active = False
        self.failed = True
        self.enabled = True
        self.exec_start = "/home/test/bin/codex-save --all-registered"
        self.masked_units = set()
        self.global_units = self.root / "global-units"
        self.runner = patch.object(self.repair, "run", side_effect=self.fake_run)
        self.runner.start()
        self.addCleanup(self.runner.stop)
        self.commands = patch.object(
            module.shutil, "which", side_effect=lambda command: f"/usr/bin/{command}"
        )
        self.commands.start()
        self.addCleanup(self.commands.stop)

    def fake_run(self, command, **_kwargs):
        self.calls.append(command)
        code, output = 0, ""
        if command[0] == "systemd-analyze":
            output = str(self.global_units) + "\n"
        elif command[:3] == ["systemctl", "--user", "show"]:
            if command[3] in self.masked_units:
                output = "masked-runtime\n"
            elif "--property=ExecStart" in command:
                output = self.exec_start
            else:
                output = "loaded\nenabled\n"
        elif command[:3] == ["systemctl", "--user", "is-enabled"]:
            code = int(not self.enabled)
        elif command[:3] == ["systemctl", "--user", "is-active"]:
            code = int(not self.active)
        elif command[:3] == ["systemctl", "--user", "is-failed"]:
            code = int(not self.failed)
        elif command[:3] == ["systemctl", "--user", "start"]:
            if command[-1] == "codex-autosave.timer":
                self.active = True
            elif command[-1] == "codex-autosave.service":
                self.failed = False
            else:
                self.fail(f"unexpected activation: {command}")
        return subprocess.CompletedProcess(command, code, output, "")

    def test_enabled_inactive_timer_and_failed_service_are_repaired(self):
        self.repair.services(captured=True)
        self.assertTrue(self.active)
        self.assertFalse(self.failed)
        starts = [call[-1] for call in self.calls if call[:3] == ["systemctl", "--user", "start"]]
        self.assertEqual(starts, ["codex-autosave.timer", "codex-autosave.service"])
        self.assertFalse(any("enable" in call or "unmask" in call for call in self.calls))
        self.assertFalse((self.repair.state / "conversation_backup_choice").exists())

    def test_failed_capture_does_not_clear_failure_or_retry_service(self):
        self.repair.services(captured=False)
        self.assertTrue(self.failed)
        self.assertNotIn(["systemctl", "--user", "start", "codex-autosave.service"], self.calls)

    def test_disabled_choice_and_disabled_timer_are_respected(self):
        choice = self.repair.state / "autoservice_choice"
        choice.write_text("no\n")
        self.repair.services(captured=True)
        self.assertEqual(self.calls, [])
        choice.write_text("yes\n")
        self.enabled = False
        self.repair.services(captured=True)
        self.assertFalse(any(call[2] == "start" for call in self.calls if len(call) > 2))

    def test_all_three_local_runtime_and_global_masks_prevent_activation(self):
        directories = [
            self.repair.config / "systemd/user",
            Path(self.env["XDG_RUNTIME_DIR"]) / "systemd/user",
            self.global_units,
        ]
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
            for unit in module.UNITS:
                with self.subTest(directory=directory, unit=unit):
                    mask = directory / unit
                    mask.symlink_to("/dev/null")
                    self.calls.clear()
                    self.repair.services(captured=True)
                    self.assertFalse(
                        any(call[:3] == ["systemctl", "--user", "start"] for call in self.calls)
                    )
                    self.assertTrue(mask.is_symlink())
                    mask.unlink()

    def test_manager_runtime_mask_is_respected(self):
        self.masked_units.add("codex-autorestore.service")
        self.repair.services(captured=True)
        self.assertFalse(any(call[:3] == ["systemctl", "--user", "start"] for call in self.calls))

    def test_unconsented_legacy_archive_is_not_started_or_consent_changed(self):
        self.exec_start = "/home/test/bin/codex-backup --archive --max-mib 2048"
        (self.repair.state / "autoservice_choice").write_text("yes\n")
        self.repair.services(captured=True)
        self.assertTrue(self.failed)
        self.assertEqual(self.repair.issues, 1)
        self.assertFalse((self.repair.state / "conversation_backup_choice").exists())

    def test_opted_in_archive_retries_existing_unit_without_changing_budgets(self):
        self.exec_start = "/home/test/bin/codex-backup --archive --max-mib 2048 --keep 2"
        choice = self.repair.state / "conversation_backup_choice"
        choice.write_text("yes\n")
        self.repair.services(captured=True)
        self.assertFalse(self.failed)
        self.assertEqual(choice.read_text(), "yes\n")
        self.assertIn("--max-mib 2048 --keep 2", self.exec_start)

    def test_monitor_opt_out_does_not_launch_processes(self):
        annotator = self.repair.install / "codex-annotator"
        annotator.touch()
        (self.repair.state / "managed_sessions").write_text("test\n")
        for key in (
            "CODEX_ANNOTATOR_ENABLED",
            "CODEX_ANNOTATOR_AUTOSTART",
            "CODEXFARM_HEALTH_ENABLED",
        ):
            with (
                self.subTest(key=key),
                patch.dict(os.environ, {key: "0"}),
                patch.object(module.subprocess, "Popen") as launch,
            ):
                self.repair.monitor()
                launch.assert_not_called()
                self.assertEqual(self.calls, [])

    def test_live_monitor_with_recent_heartbeat_is_left_running(self):
        import time

        (self.repair.install / "codex-annotator").touch()
        (self.repair.state / "managed_sessions").write_text("test\n")
        (self.repair.state / "health-status.json").write_text(
            json.dumps({"checked_at": time.time()})
        )
        with patch.object(module.subprocess, "Popen") as launch:
            self.repair.monitor()
            launch.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_monitor_refresh_never_signals_a_chat_or_an_unverified_process(self):
        proc = self.root / "proc/202"
        (proc / "fd").mkdir(parents=True)
        (proc / "fdinfo").mkdir()
        lock = self.repair.state / "annotator.lock"
        lock.touch()
        (proc / "fd/7").symlink_to(lock)
        (proc / "fdinfo/7").write_text("lock:\t1: FLOCK ADVISORY WRITE 202\n")
        (self.repair.state / "health-status.json").write_text('{"pid": 202}')
        expected = self.repair.install / "codex-annotator.py"
        for argv, held in (
            (["/usr/bin/codex", str(expected)], True),
            (["/usr/bin/python3", "/tmp/unrelated.py"], True),
            (["/usr/bin/python3", str(expected)], False),
        ):
            with self.subTest(argv=argv, held=held):
                (proc / "cmdline").write_bytes(b"\0".join(value.encode() for value in argv))
                (proc / "fdinfo/7").write_text(
                    "lock:\t1: FLOCK ADVISORY WRITE 202\n" if held else "pos:\t0\n"
                )
                with (
                    patch.dict(os.environ, {"CODEX_PROC_ROOT": str(proc.parent)}),
                    patch.object(module.os, "pidfd_open", return_value=321),
                    patch.object(module.os, "close"),
                    patch.object(module.signal, "pidfd_send_signal") as kill,
                ):
                    self.repair.stop_old_monitor(lock)
                    kill.assert_not_called()

    def test_monitor_code_update_refreshes_the_verified_installed_annotator(self):
        proc = self.root / "proc/202"
        (proc / "fd").mkdir(parents=True)
        (proc / "fdinfo").mkdir()
        lock = self.repair.state / "annotator.lock"
        lock.touch()
        (proc / "fd/7").symlink_to(lock)
        (proc / "fdinfo/7").write_text("lock:\t1: FLOCK ADVISORY WRITE 202\n")
        expected = self.repair.install / "codex-annotator.py"
        (proc / "cmdline").write_bytes(b"/usr/bin/python3\0" + str(expected).encode())
        (self.repair.state / "health-status.json").write_text('{"pid": 202}')
        with (
            patch.dict(os.environ, {"CODEX_PROC_ROOT": str(proc.parent)}),
            patch.object(module.os, "pidfd_open", return_value=321),
            patch.object(module.os, "close"),
            patch.object(module.signal, "pidfd_send_signal") as kill,
        ):
            self.repair.stop_old_monitor(lock)
            kill.assert_called_once_with(321, module.signal.SIGTERM)

    def test_current_hook_repair_preserves_custom_hooks_and_backup(self):
        # Exercise the real hook installer, isolated from the host's providers/configs.
        self.runner.stop()
        self.commands.stop()
        for name in ("codex-session-hook-install.py", "codex-session-hook.py"):
            shutil.copy2(REPO_ROOT / "bin" / name, self.repair.install / name)
        import sys

        hooks = Path(self.env["HOME"]) / ".codex/hooks.json"
        hooks.parent.mkdir(parents=True)
        previous = {
            "hooks": {
                "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "custom-hook"}]}]
            }
        }
        hooks.write_text(json.dumps(previous))

        def available(command):
            return (
                sys.executable
                if command == "python3"
                else "/stub/codex"
                if command == "codex"
                else None
            )

        with patch.object(module.shutil, "which", side_effect=available):
            self.repair.hooks()
        updated = json.loads(hooks.read_text())
        self.assertEqual(
            updated["hooks"]["UserPromptSubmit"][0], previous["hooks"]["UserPromptSubmit"][0]
        )
        self.assertIn("SessionStart", updated["hooks"])
        backups = list((self.repair.state / "doctor-repairs").glob("*/hooks/codex.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), previous)
