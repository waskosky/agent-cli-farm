from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "integrations" / "install_tmux_deep_history.py"


def build_archive(path: Path, *, unsafe_name: str | None = None) -> str:
    files = {
        "tmux-deep-history/VERSION": (b"0.1.0\n", 0o644),
        "tmux-deep-history/tmux-deep-history.tmux": (b"#!/usr/bin/env bash\n", 0o755),
        "tmux-deep-history/bin/tmux-deep-history": (b"#!/usr/bin/env bash\n", 0o755),
        "tmux-deep-history/src/tmux_deep_history/__init__.py": (b"", 0o644),
        "tmux-deep-history/src/tmux_deep_history/tmux.py": (
            b"class TmuxError(RuntimeError):\n"
            b"    pass\n"
            b"class PaneInfo:\n"
            b"    def __init__(self, **values):\n"
            b"        self.__dict__.update(values)\n"
            b"class Tmux:\n"
            b"    def start_pipe(self, target, command, *, only_if_none=True):\n"
            b"        pass\n",
            0o644,
        ),
        "tmux-deep-history/src/tmux_deep_history/cli.py": (
            b"import sys\nprint('arguments=' + ' '.join(sys.argv[1:]))\n",
            0o644,
        ),
    }
    if unsafe_name is not None:
        files[unsafe_name] = (b"unsafe\n", 0o644)
    with zipfile.ZipFile(path, "w") as archive:
        for name, (content, mode) in files.items():
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, content)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_lock(path: Path, digest: str) -> None:
    path.write_text(
        f"repository=waskosky/tmux-deep-history\nversion=0.1.0\nsha256={digest}\n",
        encoding="utf-8",
    )


class DeepHistoryInstallerTests(unittest.TestCase):
    def run_installer(
        self, *, archive: Path, lock: Path, destination: Path
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(INSTALLER),
                "--archive",
                str(archive),
                "--lock-file",
                str(lock),
                "--destination",
                str(destination),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_installs_and_atomically_updates_pinned_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "release.zip"
            lock = root / "release.lock"
            destination = root / "data" / "tmux-deep-history"
            write_lock(lock, build_archive(archive))

            first = self.run_installer(archive=archive, lock=lock, destination=destination)
            self.assertEqual(first.returncode, 0, first.stderr)
            cli = destination / "bin" / "tmux-deep-history"
            self.assertTrue(cli.is_file())
            self.assertNotEqual(cli.stat().st_mode & 0o111, 0)
            self.assertTrue((destination / "bin" / "tmux-deep-history-upstream").is_file())
            configured_python = destination / "bin" / ".codexfarm-python"
            self.assertEqual(
                configured_python.read_text(encoding="utf-8").strip(),
                str(Path(sys.executable).resolve()),
            )
            self.assertEqual(configured_python.stat().st_mode & 0o777, 0o600)
            launched = subprocess.run(
                [cli, "status"],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(launched.returncode, 0, launched.stderr)
            self.assertEqual(launched.stdout.strip(), "arguments=status")

            (destination / "obsolete.txt").write_text("old\n", encoding="utf-8")
            second = self.run_installer(archive=archive, lock=lock, destination=destination)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertFalse((destination / "obsolete.txt").exists())
            self.assertEqual((destination / "VERSION").read_text(encoding="utf-8"), "0.1.0\n")

    def test_launcher_falls_back_to_supported_versioned_python(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "release.zip"
            lock = root / "release.lock"
            destination = root / "data" / "tmux-deep-history"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            write_lock(lock, build_archive(archive))

            result = self.run_installer(archive=archive, lock=lock, destination=destination)
            self.assertEqual(result.returncode, 0, result.stderr)
            (destination / "bin" / ".codexfarm-python").unlink()

            (fake_bin / "python3").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            (fake_bin / "python3").chmod(0o755)
            (fake_bin / "python3.12").write_text(
                '#!/bin/sh\nprintf \'%s\\n\' "$0" > "$SELECTED_PYTHON_LOG"\n',
                encoding="utf-8",
            )
            (fake_bin / "python3.12").chmod(0o755)
            selected = root / "selected-python"
            env = os.environ.copy()
            env.pop("TMUX_DEEP_HISTORY_PYTHON", None)
            env.pop("CODEXFARM_DEEP_HISTORY_PYTHON_BIN", None)
            env.pop("CODEXFARM_PYTHON_BIN", None)
            env["PATH"] = f"{fake_bin}:/usr/bin:/bin"
            env["SELECTED_PYTHON_LOG"] = str(selected)

            launched = subprocess.run(
                [destination / "bin" / "tmux-deep-history", "status"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(launched.returncode, 0, launched.stderr)
            self.assertEqual(
                selected.read_text(encoding="utf-8").strip(), str(fake_bin / "python3.12")
            )

    def test_checksum_failure_preserves_previous_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "release.zip"
            lock = root / "release.lock"
            destination = root / "data" / "tmux-deep-history"
            destination.mkdir(parents=True)
            sentinel = destination / "keep.txt"
            sentinel.write_text("keep\n", encoding="utf-8")
            build_archive(archive)
            write_lock(lock, "0" * 64)

            result = self.run_installer(archive=archive, lock=lock, destination=destination)

            self.assertEqual(result.returncode, 1)
            self.assertIn("checksum mismatch", result.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")

    def test_rejects_symlink_destination_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "release.zip"
            lock = root / "release.lock"
            target = root / "existing-install"
            destination = root / "tmux-deep-history"
            target.mkdir()
            sentinel = target / "keep.txt"
            sentinel.write_text("keep\n", encoding="utf-8")
            destination.symlink_to(target, target_is_directory=True)
            write_lock(lock, build_archive(archive))

            result = self.run_installer(archive=archive, lock=lock, destination=destination)

            self.assertEqual(result.returncode, 1)
            self.assertIn("refusing symbolic-link destination", result.stderr)
            self.assertTrue(destination.is_symlink())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")

    def test_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "release.zip"
            lock = root / "release.lock"
            destination = root / "data" / "tmux-deep-history"
            write_lock(lock, build_archive(archive, unsafe_name="tmux-deep-history/../../escape"))

            result = self.run_installer(archive=archive, lock=lock, destination=destination)

            self.assertEqual(result.returncode, 1)
            self.assertIn("unsafe ZIP member", result.stderr)
            self.assertFalse((root / "escape").exists())

    def test_does_not_change_existing_destination_parent_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "release.zip"
            lock = root / "release.lock"
            destination = root / "tmux-deep-history"
            root.chmod(0o755)
            write_lock(lock, build_archive(archive))

            result = self.run_installer(archive=archive, lock=lock, destination=destination)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(root.stat().st_mode & 0o777, 0o755)


if __name__ == "__main__":
    unittest.main()
