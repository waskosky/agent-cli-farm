from __future__ import annotations

import argparse
import importlib.util
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("farm_launchd", ROOT / "bin/codex-launchd.py")
launchd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launchd)


class LaunchdTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="farm launchd & ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state/codexfarm"
        self.calls = []
        self.loaded = {}
        self.disabled = "disabled services = {}"
        self.fail_bootstrap = False
        self.args = argparse.Namespace(
            state_dir=str(self.state),
            save_only=False,
            archives="no",
            save_bin=str(self.root / "helpers/codex-save"),
            backup_bin=str(self.root / "helpers/codex-backup"),
            restore_bin=str(self.root / "helpers/codex-restore"),
        )
        for context in (
            patch.dict(os.environ, {"HOME": str(self.root), "PATH": "/nvm & tools:/usr/bin:/bin"}),
            patch.object(launchd, "GLOBAL_DIRS", (self.root / "global",)),
            patch.object(launchd.subprocess, "run", side_effect=self.run_manager),
        ):
            context.start()
            self.addCleanup(context.stop)

    def run_manager(self, argv, **kwargs):
        self.calls.append(argv[1:])
        action = argv[1]
        output, code = "", 0
        if action == "print-disabled":
            output = self.disabled
        elif action == "print":
            label = argv[2].rsplit("/", 1)[-1]
            output = self.loaded.get(label, "")
            code = 0 if label in self.loaded else 113
        elif action == "bootstrap":
            code = 5 if self.fail_bootstrap else 0
            if not code:
                self.loaded[Path(argv[3]).stem] = "state = not running\nlast exit code = 0"
        return subprocess.CompletedProcess(argv, code, output, "")

    def read_plist(self, label):
        return plistlib.loads((launchd.agent_dir() / f"{label}.plist").read_bytes())

    def test_install_preserves_exact_paths_private_files_and_schedules_without_restoring_now(self):
        launchd.install(self.args)
        saved = self.read_plist(launchd.SAVE_LABEL)
        restore = self.read_plist(launchd.RESTORE_LABEL)
        self.assertEqual(
            saved["ProgramArguments"], [self.args.save_bin, "--all-registered", "--best-effort"]
        )
        self.assertEqual(saved["EnvironmentVariables"]["PATH"], os.environ["PATH"])
        self.assertNotIn("LAUNCHCTL_BIN", saved["EnvironmentVariables"])
        self.assertEqual(saved["StartCalendarInterval"], [{"Minute": n} for n in range(0, 60, 5)])
        self.assertEqual(saved["Umask"], 0o077)
        self.assertEqual(saved["Nice"], 10)
        self.assertTrue(saved["LowPriorityIO"])
        self.assertTrue(restore["AbandonProcessGroup"])
        self.assertEqual(
            restore["ProgramArguments"],
            [self.args.restore_bin, "--all-registered", "--skip-active"],
        )
        self.assertEqual(
            [call for call in self.calls if call[0] == "bootstrap"],
            [
                [
                    "bootstrap",
                    launchd.domain(),
                    str(launchd.agent_dir() / f"{launchd.SAVE_LABEL}.plist"),
                ]
            ],
        )
        for path in list(launchd.agent_dir().glob("*.plist")) + list(self.state.glob("*choice")):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.state / "conversation_backup_choice").read_text(), "no\n")

    def test_save_only_preserves_existing_restore_definition_and_registration(self):
        path = launchd.agent_dir() / f"{launchd.RESTORE_LABEL}.plist"
        launchd.atomic_write(
            path, plistlib.dumps({"Label": launchd.RESTORE_LABEL, "RunAtLoad": True})
        )
        before = path.read_bytes()
        self.loaded[launchd.RESTORE_LABEL] = "pid = 999"
        self.args.save_only = True
        launchd.install(self.args)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(
            any(
                call[0] in ("bootstrap", "bootout") and launchd.RESTORE_LABEL in " ".join(call)
                for call in self.calls
            )
        )

    def test_archive_choice_selects_only_explicit_archive_command(self):
        self.args.archives = "yes"
        launchd.install(self.args)
        self.assertEqual(
            self.read_plist(launchd.SAVE_LABEL)["ProgramArguments"],
            [self.args.backup_bin, "--archive", "--min-age", "3600"],
        )
        self.assertEqual((self.state / "conversation_backup_choice").read_text(), "yes\n")

    def test_disabled_jobs_refuse_before_any_writes_and_never_enable(self):
        for label in launchd.LABELS:
            for value in ("disabled", "true"):
                self.disabled = f'"{label}" => {value}'
                with self.assertRaisesRegex(ValueError, "preserving operator"):
                    launchd.install(self.args)
                self.assertFalse(self.state.exists())
                self.assertFalse(launchd.agent_dir().exists())
        self.assertTrue(all(call[0] == "print-disabled" for call in self.calls))

    def test_plist_masks_and_global_definitions_are_preserved(self):
        for directory in (launchd.agent_dir(), self.root / "global"):
            for label in launchd.LABELS:
                for kind in ("symlink", "disabled", "empty"):
                    path = directory / f"{label}.plist"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if kind == "symlink":
                        path.symlink_to("/dev/null")
                    else:
                        path.write_bytes(
                            b""
                            if kind == "empty"
                            else plistlib.dumps({"Label": label, "Disabled": True})
                        )
                    with self.assertRaises((ValueError, plistlib.InvalidFileException)):
                        launchd.install(self.args)
                    self.assertFalse(self.state.exists())
                    path.unlink()

    def test_running_save_is_not_interrupted(self):
        self.loaded[launchd.SAVE_LABEL] = "state = running\npid = 42"
        with self.assertRaisesRegex(ValueError, "running"):
            launchd.install(self.args)
        self.assertFalse(self.state.exists())
        self.assertFalse(any(call[0] == "bootout" for call in self.calls))

    def test_failed_activation_is_not_reported_as_success(self):
        self.fail_bootstrap = True
        with self.assertRaisesRegex(ValueError, "bootstrap failed"):
            launchd.install(self.args)
        self.assertEqual((self.state / "autoservice_choice").read_text(), "yes\n")
        self.assertEqual(launchd.status(), 1)

    def test_status_distinguishes_loaded_idle_from_last_save_failure(self):
        self.assertEqual(launchd.status(), 1)
        self.loaded[launchd.SAVE_LABEL] = "state = not running\nlast exit code = 0"
        self.assertEqual(launchd.status(), 0)
        self.loaded[launchd.SAVE_LABEL] = "state = not running\nlast exit code = 1"
        self.assertEqual(launchd.status(), 1)


if __name__ == "__main__":
    unittest.main()
