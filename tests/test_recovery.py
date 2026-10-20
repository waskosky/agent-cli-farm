"""Recovery drills use synthetic data and fresh private trees only."""

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_looper import recovery
from codex_looper.resource_config import write_private_json


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o700)
        self.archive = self.root / "snapshot-fixture.tar.gz"
        environment = patch.dict(
            os.environ,
            {
                "HOME": str(self.root / "home"),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_STATE_HOME": str(self.root / "state"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

    def make_archive(self, files=None, *, extra=(), inventory=None):
        files = (
            {
                "codex/sessions/rollout-fixture.jsonl": b'{"message":"private fixture"}\n',
                "farm/manifest.tsv": b"name\tdir\tcmd\targs\nfixture\t/private\tclaude\t--resume 123e4567-e89b-42d3-a456-426614174000\n",
            }
            if files is None
            else files
        )
        with tarfile.open(self.archive, "w:gz") as bundle:
            for name, content in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                bundle.addfile(member, io.BytesIO(content))
            for member in extra:
                bundle.addfile(member)
            data = json.dumps(
                {
                    "schema": 1,
                    "manifest_save_ok": True,
                    "files": inventory
                    if inventory is not None
                    else [{"path": name, "bytes": len(content)} for name, content in files.items()],
                }
            ).encode()
            member = tarfile.TarInfo("snapshot.json")
            member.size = len(data)
            bundle.addfile(member, io.BytesIO(data))
        self.receipt()

    def receipt(self):
        digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        write_private_json(
            self.archive.with_name(self.archive.name + ".integrity.json"),
            {
                "schema_version": 1,
                "archive": self.archive.name,
                "sha256": digest,
                "bytes": self.archive.stat().st_size,
            },
        )

    def test_verify_and_extract_complete_private_restore_without_replacing_live_data(self):
        self.make_archive()
        result = recovery.verify_archive(self.archive)
        self.assertTrue(result["verified"])
        self.assertEqual(result["files"], 2)
        target = self.root / "restored"
        recovery.verify_archive(self.archive, destination=target)
        self.assertIn(
            "private fixture", (target / "codex/sessions/rollout-fixture.jsonl").read_text()
        )
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)
        self.assertEqual((target / "farm/manifest.tsv").stat().st_mode & 0o777, 0o600)
        before = (target / "farm/manifest.tsv").read_bytes()
        with self.assertRaises(FileExistsError):
            recovery.verify_archive(self.archive, destination=target)
        self.assertEqual((target / "farm/manifest.tsv").read_bytes(), before)

    def test_bad_checksum_missing_receipt_and_bad_inventory_fail_before_destination_creation(self):
        self.make_archive()
        target = self.root / "restored"
        with self.assertRaisesRegex(ValueError, "checksum"):
            recovery.verify_archive(self.archive, expected="0" * 64, destination=target)
        self.assertFalse(target.exists())
        self.archive.with_name(self.archive.name + ".integrity.json").unlink()
        with self.assertRaisesRegex(ValueError, "checksum receipt"):
            recovery.verify_archive(self.archive)
        self.make_archive(inventory=[])
        with self.assertRaisesRegex(ValueError, "inventory"):
            recovery.verify_archive(self.archive, destination=target)
        self.assertFalse(target.exists())

    def test_unsafe_paths_links_and_special_files_cannot_escape_restore_tree(self):
        for name in ("../escaped", "/tmp/escaped", "a/../../escaped", "a\\escaped", "C:/escaped"):
            with self.subTest(name=name):
                self.make_archive({name: b"bad"})
                with self.assertRaises(ValueError):
                    recovery.verify_archive(self.archive, destination=self.root / "restored")
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE):
            member = tarfile.TarInfo("link")
            member.type, member.linkname = kind, "../escaped"
            self.make_archive(extra=[member])
            with self.assertRaises(ValueError):
                recovery.verify_archive(self.archive)
        self.assertFalse((self.root / "escaped").exists())

    def test_duplicate_members_and_uncompressed_budget_fail(self):
        duplicate = tarfile.TarInfo("farm/manifest.tsv")
        self.make_archive(extra=[duplicate])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            recovery.verify_archive(self.archive)
        self.make_archive({"large": b"x" * 100000})
        with self.assertRaisesRegex(ValueError, "uncompressed"):
            recovery.verify_archive(self.archive, max_bytes=10000)

    def test_sqlite_integrity_is_checked_only_in_private_restore(self):
        database = self.root / "fixture.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE records(value TEXT)")
            connection.execute("INSERT INTO records VALUES ('fixture')")
        self.make_archive({"codex/state_5.sqlite": database.read_bytes()})
        result = recovery.verify_archive(self.archive)
        self.assertEqual(result["databases_checked"], 1)
        self.make_archive({"codex/state_5.sqlite": b"not a database"})
        target = self.root / "invalid-database"
        with self.assertRaises(sqlite3.Error):
            recovery.verify_archive(self.archive, destination=target)
        self.assertFalse(target.exists())

    def test_failed_extraction_preserves_existing_paths_and_cleans_own_partial_tree(self):
        self.make_archive()
        target = self.root / "restored"
        with patch.object(recovery, "check_databases", side_effect=ValueError("fixture failure")):
            with self.assertRaises(ValueError):
                recovery.verify_archive(self.archive, destination=target)
        self.assertFalse(target.exists())
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        (unrelated / "keep").write_text("keep")
        target.symlink_to(unrelated)
        with self.assertRaises(FileExistsError):
            recovery.verify_archive(self.archive, destination=target)
        self.assertEqual((unrelated / "keep").read_text(), "keep")

    def test_verified_copy_is_idempotent_and_does_not_replace_operator_data(self):
        self.make_archive()
        destination = self.root / "external-copy"
        result = recovery.copy_archive(self.archive, destination)
        self.assertTrue(result["copy_verified"])
        self.assertEqual((destination / self.archive.name).read_bytes(), self.archive.read_bytes())
        self.assertTrue(recovery.copy_archive(self.archive, destination)["copy_verified"])
        (destination / self.archive.name).write_bytes(b"operator data")
        with self.assertRaises(ValueError):
            recovery.copy_archive(self.archive, destination)
        self.assertEqual((destination / self.archive.name).read_bytes(), b"operator data")
        self.assertFalse(list(destination.glob(".copy-*")))

    def test_no_checksum_or_tar_failure_is_treated_as_a_verified_copy(self):
        self.make_archive()
        self.archive.write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            recovery.copy_archive(self.archive, self.root / "external-copy")
        self.assertFalse((self.root / "external-copy").exists())

    def test_copy_retention_prunes_only_unchanged_indexed_archives(self):
        destination = self.root / "copies"
        for number in range(4):
            self.archive = self.root / f"snapshot-{number}.tar.gz"
            self.make_archive()
            result = recovery.copy_archive(self.archive, destination, keep=1)
            if number == 0:
                (destination / self.archive.name).write_bytes(b"operator replacement")
                (destination / "unindexed.tar.gz").write_bytes(b"operator archive")
        self.assertEqual((destination / "snapshot-0.tar.gz").read_bytes(), b"operator replacement")
        self.assertEqual((destination / "unindexed.tar.gz").read_bytes(), b"operator archive")
        self.assertFalse((destination / "snapshot-1.tar.gz").exists())
        self.assertFalse((destination / "snapshot-2.tar.gz").exists())
        self.assertTrue((destination / "snapshot-3.tar.gz").is_file())
        self.assertEqual(result["retained_copies"], 2)
        self.assertEqual(result["preserved_operator_files"], 1)

    def test_workspace_preserves_uncommitted_and_exact_selected_untracked_files(self):
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
        (repo / "tracked.py").write_text("original")
        (repo / ".env").write_text("secret must not be copied")
        subprocess.run(["git", "-C", str(repo), "add", "tracked.py", ".env"], check=True)
        (repo / "tracked.py").write_text("uncommitted work")
        (repo / "selected.txt").write_text("selected untracked")
        (repo / "unselected.txt").write_text("unselected untracked")
        output = self.root / "workspaces" / "workspace.tar.gz"
        result = recovery.workspace_snapshot(repo, output, include_untracked=["selected.txt"])
        self.assertTrue(result["verified"])
        target = self.root / "workspace-restore"
        recovery.verify_archive(output, destination=target)
        restored = target / "workspaces/repository"
        self.assertEqual((restored / "tracked.py").read_text(), "uncommitted work")
        self.assertEqual((restored / "selected.txt").read_text(), "selected untracked")
        self.assertFalse((restored / "unselected.txt").exists())
        self.assertFalse((restored / ".env").exists())
        self.assertFalse((restored / ".git").exists())
        with self.assertRaises(ValueError):
            recovery.workspace_snapshot(repo, output)

    def test_workspace_selection_rejects_symlink_escape_and_missing_untracked_file(self):
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
        (repo / "source").write_text("source")
        subprocess.run(["git", "-C", str(repo), "add", "source"], check=True)
        (repo / "escape").symlink_to(self.root)
        for name in ("../outside", "escape/source", "missing"):
            with self.subTest(name=name), self.assertRaises((OSError, ValueError)):
                recovery.workspace_snapshot(
                    repo, self.root / "workspace.tar.gz", include_untracked=[name]
                )
        self.assertFalse((self.root / "workspace.tar.gz").exists())
