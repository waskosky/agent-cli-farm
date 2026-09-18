from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.codex = self.root / "codex"
        self.sessions = self.codex / "sessions/2026/09/18"
        self.sessions.mkdir(parents=True)
        (self.sessions / "rollout-chat.jsonl").write_text('{"message":"recover me"}\n')
        (self.codex / "auth.json").write_text("SECRET MUST NOT BE COPIED")
        (self.codex / "config.toml").write_text("SECRET MUST NOT BE COPIED")
        self.destination = self.root / "backup"
        self.env = {
            **os.environ,
            "CODEX_HOME": str(self.codex),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
        }
        self.save = self.root / "save"
        self.save.write_text("#!/usr/bin/env bash\nexit 0\n")
        self.save.chmod(0o700)
        self.env["CODEX_SAVE_BIN"] = str(self.save)

    def tearDown(self):
        self.temporary.cleanup()

    def run_backup(self, *args):
        return subprocess.run(
            [
                sys.executable,
                ROOT / "bin/codex-backup",
                "--destination",
                self.destination,
                *args,
            ],
            env=self.env,
            capture_output=True,
            text=True,
        )

    def test_snapshot_preserves_uncheckpointed_wal_and_excludes_credentials(self):
        connection = sqlite3.connect(self.codex / "state_5.sqlite")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE threads(id TEXT)")
        connection.execute("INSERT INTO threads VALUES ('saved-thread')")
        connection.commit()
        try:
            result = self.run_backup()
            self.assertEqual(result.returncode, 0, result.stderr)
            archive = next(self.destination.glob("*.tar.gz"))
            with tarfile.open(archive) as bundle:
                self.assertNotIn("codex/auth.json", bundle.getnames())
                self.assertNotIn("codex/config.toml", bundle.getnames())
                recovered = self.root / "recovered.sqlite"
                recovered.write_bytes(bundle.extractfile("codex/state_5.sqlite").read())
                self.assertIn(
                    b"recover me",
                    bundle.extractfile("codex/sessions/2026/09/18/rollout-chat.jsonl").read(),
                )
            restored = sqlite3.connect(recovered)
            self.assertEqual(
                restored.execute("SELECT id FROM threads").fetchall(),
                [("saved-thread",)],
            )
            restored.close()
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.destination.stat().st_mode & 0o777, 0o700)
        finally:
            connection.close()

    def test_failed_manifest_save_still_preserves_chats_and_reports_failure(self):
        self.save.write_text("#!/usr/bin/env bash\nexit 1\n")
        result = self.run_backup()
        self.assertEqual(result.returncode, 1)
        self.assertIn("preserving conversation history", result.stderr)
        self.assertEqual(len(list(self.destination.glob("*.tar.gz"))), 1)
        status = json.loads((self.root / "state/codexfarm/backup-status.json").read_text())
        self.assertTrue(status["backup_ok"])
        self.assertFalse(status["manifest_save_ok"])

    def test_failed_database_snapshot_preserves_prior_archive(self):
        self.assertEqual(self.run_backup().returncode, 0)
        previous = list(self.destination.glob("*.tar.gz"))
        (self.codex / "state_5.sqlite").write_text("broken database")
        result = self.run_backup("--keep", "1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(list(self.destination.glob("*.tar.gz")), previous)
        self.assertFalse(list(self.destination.glob(".snapshot-*")))
        self.assertFalse(list(self.destination.glob(".database-*")))

    def test_retention_and_min_age_preserve_unrelated_files(self):
        self.assertEqual(self.run_backup().returncode, 0)
        previous = list(self.destination.glob("*.tar.gz"))
        self.assertEqual(self.run_backup("--min-age", "3600").returncode, 0)
        self.assertEqual(list(self.destination.glob("*.tar.gz")), previous)
        unrelated = self.destination / "snapshot-personal.tar.gz"
        unrelated.write_text("keep this")
        self.assertEqual(self.run_backup("--keep", "1").returncode, 0)
        self.assertTrue(unrelated.exists())
        self.assertFalse(previous[0].exists())

    def test_symlink_outside_history_is_not_archived(self):
        outside = self.root / "outside.jsonl"
        outside.write_text("private unrelated content")
        (self.sessions / "escape.jsonl").symlink_to(outside)
        self.assertEqual(self.run_backup().returncode, 0)
        with tarfile.open(next(self.destination.glob("*.tar.gz"))) as bundle:
            self.assertNotIn("codex/sessions/2026/09/18/escape.jsonl", bundle.getnames())

    def test_invalid_arguments_fail_cleanly(self):
        result = self.run_backup("--keep", "0")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)

    def test_fallback_watcher_exits_when_systemd_timer_is_active(self):
        systemctl = self.root / "systemctl"
        systemctl.write_text("#!/bin/sh\nexit 0\n")
        systemctl.chmod(0o700)
        self.env["PATH"] = str(self.root) + os.pathsep + self.env["PATH"]
        result = self.run_backup("--watch")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fallback watcher exiting", result.stdout)
        self.assertFalse((self.root / "state/codexfarm/backup-watch.json").exists())
        self.assertFalse(list(self.destination.glob("*.tar.gz")))

    def test_lost_disk_headroom_preserves_prior_archive_and_cleans_partial_file(self):
        self.assertEqual(self.run_backup().returncode, 0)
        previous = list(self.destination.glob("*.tar.gz"))
        latest = (self.destination / "latest.json").read_bytes()
        loader = importlib.machinery.SourceFileLoader("backup_test", str(ROOT / "bin/codex-backup"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        with patch.object(
            module.shutil,
            "disk_usage",
            side_effect=[
                SimpleNamespace(free=10**12),
                SimpleNamespace(free=0),
            ],
        ):
            with self.assertRaises(module.BackupSpaceError):
                module.snapshot(self.codex, self.root / "config", self.destination, 1, True)
        self.assertEqual(list(self.destination.glob("*.tar.gz")), previous)
        self.assertEqual((self.destination / "latest.json").read_bytes(), latest)
        self.assertFalse(list(self.destination.glob(".snapshot-*")))

    def test_database_is_staged_before_large_transcripts(self):
        connection = sqlite3.connect(self.codex / "state_5.sqlite")
        connection.execute("CREATE TABLE threads(id TEXT)")
        connection.close()
        self.assertEqual(self.run_backup().returncode, 0)
        with tarfile.open(next(self.destination.glob("*.tar.gz"))) as bundle:
            self.assertEqual(bundle.getnames()[0], "codex/state_5.sqlite")

    def test_invalid_snapshot_timestamp_does_not_hide_stale_backup(self):
        self.assertEqual(self.run_backup().returncode, 0)
        latest = self.destination / "latest.json"
        value = json.loads(latest.read_text())
        value["created_at"] = "nan"
        latest.write_text(json.dumps(value))
        self.assertEqual(self.run_backup("--min-age", "3600").returncode, 0)
        self.assertEqual(len(list(self.destination.glob("*.tar.gz"))), 2)
