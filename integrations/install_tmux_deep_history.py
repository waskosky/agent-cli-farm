#!/usr/bin/env python3
"""Install the checksum-pinned tmux-deep-history release atomically."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import stat
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

PROJECT_NAME = "tmux-deep-history"
LAUNCHER_NAME = "tmux_deep_history_launcher.sh"
LOCK_KEYS = {"repository", "version", "sha256"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){2}(?:[-+][A-Za-z0-9.-]+)?$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class InstallError(RuntimeError):
    """An expected installation or validation failure."""


def default_destination() -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return data_home / "codexfarm" / "plugins" / PROJECT_NAME


def read_lock(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise InstallError(f"unable to read lock file {path}: {exc}") from exc
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in LOCK_KEYS or not value:
            raise InstallError(f"invalid lock entry at {path}:{line_number}")
        if key in values:
            raise InstallError(f"duplicate lock key {key!r} at {path}:{line_number}")
        values[key] = value
    missing = LOCK_KEYS.difference(values)
    if missing:
        raise InstallError(f"lock file is missing: {', '.join(sorted(missing))}")
    if not REPOSITORY_RE.fullmatch(values["repository"]):
        raise InstallError("lock repository is invalid")
    if not VERSION_RE.fullmatch(values["version"]):
        raise InstallError("lock version is invalid")
    if not SHA256_RE.fullmatch(values["sha256"]):
        raise InstallError("lock sha256 is invalid")
    return values


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def release_url(lock: dict[str, str]) -> str:
    version = lock["version"]
    return (
        f"https://github.com/{lock['repository']}/releases/download/v{version}/"
        f"{PROJECT_NAME}-v{version}.zip"
    )


def download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "agent-cli-farm-setup"})
    try:
        with (
            urllib.request.urlopen(request, timeout=60) as response,
            destination.open("wb") as output,
        ):
            shutil.copyfileobj(response, output, length=1024 * 1024)
    except (OSError, urllib.error.URLError) as exc:
        raise InstallError(f"unable to download {url}: {exc}") from exc


def validated_member_path(info: zipfile.ZipInfo) -> PurePosixPath:
    member = PurePosixPath(info.filename)
    if member.is_absolute() or ".." in member.parts:
        raise InstallError(f"unsafe ZIP member: {info.filename}")
    if not member.parts or member.parts[0] != PROJECT_NAME:
        raise InstallError(f"unexpected ZIP member root: {info.filename}")
    mode = (info.external_attr >> 16) & 0xFFFF
    if stat.S_ISLNK(mode):
        raise InstallError(f"release ZIP contains a symbolic link: {info.filename}")
    return member


def extract_release(archive_path: Path, destination: Path) -> Path:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            bad_member = archive.testzip()
            if bad_member:
                raise InstallError(f"corrupt ZIP member: {bad_member}")
            for info in archive.infolist():
                member = validated_member_path(info)
                target = destination.joinpath(*member.parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                archived_mode = (info.external_attr >> 16) & 0o777
                target.chmod(archived_mode or 0o644)
    except (OSError, zipfile.BadZipFile) as exc:
        raise InstallError(f"unable to extract {archive_path}: {exc}") from exc
    return destination / PROJECT_NAME


def install_release(
    *,
    lock_path: Path,
    destination: Path,
    archive_path: Path | None = None,
    launcher_path: Path | None = None,
) -> tuple[Path, str]:
    lock = read_lock(lock_path)
    destination = Path(os.path.abspath(os.fspath(destination.expanduser())))
    if destination.is_symlink():
        raise InstallError(f"refusing symbolic-link destination: {destination}")
    parent_existed = destination.parent.exists()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        destination.parent.chmod(0o700)

    with tempfile.TemporaryDirectory(
        prefix=".tmux-deep-history-install-", dir=destination.parent
    ) as tmp:
        work_dir = Path(tmp)
        archive = work_dir / f"{PROJECT_NAME}.zip"
        if archive_path is None:
            download(release_url(lock), archive)
        else:
            try:
                shutil.copyfile(archive_path.expanduser().resolve(), archive)
            except OSError as exc:
                raise InstallError(f"unable to read release archive {archive_path}: {exc}") from exc

        actual_hash = file_sha256(archive)
        if actual_hash != lock["sha256"]:
            raise InstallError(
                f"release checksum mismatch: expected {lock['sha256']}, got {actual_hash}"
            )

        staged = extract_release(archive, work_dir / "extracted")
        version_file = staged / "VERSION"
        try:
            archive_version = version_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise InstallError("release archive is missing VERSION") from exc
        if archive_version != lock["version"]:
            raise InstallError(
                f"release version mismatch: expected {lock['version']}, got {archive_version}"
            )
        required = (staged / "tmux-deep-history.tmux", staged / "bin" / PROJECT_NAME)
        if not all(path.is_file() for path in required):
            raise InstallError("release archive is missing required plugin entrypoints")
        if not os.access(required[1], os.X_OK):
            raise InstallError("release CLI entrypoint is not executable")

        launcher = launcher_path or Path(__file__).resolve().with_name(LAUNCHER_NAME)
        if not launcher.is_file():
            raise InstallError(f"deep-history compatibility launcher is missing: {launcher}")
        upstream_cli = required[1].with_name(f"{PROJECT_NAME}-upstream")
        try:
            os.replace(required[1], upstream_cli)
            shutil.copyfile(launcher, required[1])
            required[1].chmod(0o755)
            configured_python = required[1].with_name(".codexfarm-python")
            configured_python.write_text(f"{Path(sys.executable).resolve()}\n", encoding="utf-8")
            configured_python.chmod(0o600)
        except OSError as exc:
            raise InstallError(f"unable to install deep-history compatibility launcher: {exc}") from exc

        backup = work_dir / "previous-install"
        had_previous = destination.exists() or destination.is_symlink()
        if had_previous:
            os.replace(destination, backup)
        try:
            os.replace(staged, destination)
        except Exception:
            if had_previous and backup.exists():
                os.replace(backup, destination)
            raise

    return destination, lock["version"]


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock-file", type=Path, default=root / "tmux-deep-history.lock")
    parser.add_argument("--destination", type=Path, default=default_destination())
    parser.add_argument(
        "--archive", type=Path, help="use a local release ZIP instead of downloading"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        destination, version = install_release(
            lock_path=args.lock_file,
            destination=args.destination,
            archive_path=args.archive,
        )
    except InstallError as exc:
        print(f"tmux-deep-history installation failed: {exc}", file=sys.stderr)
        return 1
    print(f"Installed tmux-deep-history {version} at {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
