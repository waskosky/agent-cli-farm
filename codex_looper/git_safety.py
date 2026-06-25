from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .models import ConfigError


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )


def create_backup_branch(
    cwd: Path,
    *,
    prefix: str,
    loop_number: int,
    stamp: str | None = None,
) -> str:
    clean_prefix = prefix.rstrip("/")
    if not clean_prefix:
        raise ConfigError("backup_prefix must not be empty")
    branch = f"{clean_prefix}/{stamp or _utc_stamp()}-loop-{loop_number:04d}"
    try:
        _run_git(cwd, "branch", branch, "HEAD")
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or exc.stdout or str(exc)).strip()
        raise ConfigError(f"failed to create backup branch {branch}: {message}") from exc
    return branch


def prune_backup_branches(cwd: Path, *, prefix: str, keep: int) -> list[str]:
    if keep <= 0:
        return []
    clean_prefix = prefix.rstrip("/")
    if not clean_prefix:
        raise ConfigError("backup_prefix must not be empty")
    ref_prefix = f"refs/heads/{clean_prefix}/"
    try:
        result = _run_git(cwd, "for-each-ref", "--format=%(refname:short)", ref_prefix)
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or exc.stdout or str(exc)).strip()
        raise ConfigError(f"failed to list backup branches: {message}") from exc
    branches = sorted(line for line in result.stdout.splitlines() if line)
    to_delete = branches[:-keep]
    for branch in to_delete:
        try:
            _run_git(cwd, "branch", "-D", branch)
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or str(exc)).strip()
            raise ConfigError(f"failed to prune backup branch {branch}: {message}") from exc
    return to_delete


def git_workspace_fingerprint(cwd: Path, ignored_paths: list[Path] | None = None) -> str | None:
    try:
        head = _run_git(cwd, "rev-parse", "HEAD").stdout.strip()
        status_result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=cwd,
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return None

    cwd_resolved = cwd.resolve()
    ignored_prefixes: list[str] = []
    for path in ignored_paths or []:
        try:
            relative = path if not path.is_absolute() else path.resolve().relative_to(cwd_resolved)
        except ValueError:
            continue
        normalized = str(relative).strip("/")
        if normalized:
            ignored_prefixes.append(normalized)

    digest = hashlib.sha256()
    digest.update(f"HEAD {head}\0".encode())
    records = status_result.stdout.split(b"\0")
    index = 0
    while index < len(records):
        raw = records[index]
        index += 1
        if not raw:
            continue
        entry = raw.decode("utf-8", errors="surrogateescape")
        code = entry[:2]
        status_path = entry[3:]
        stable_entry = entry
        if code[:1] in {"R", "C"} or code[1:2] in {"R", "C"}:
            if index < len(records):
                old_path = records[index].decode("utf-8", errors="surrogateescape")
                index += 1
                stable_entry = f"{entry}\0{old_path}"
        path = status_path
        if any(
            status_path == prefix or status_path.startswith(f"{prefix}/")
            for prefix in ignored_prefixes
        ):
            continue
        digest.update(stable_entry.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")

        full_path = cwd / path
        try:
            stat_result = full_path.lstat()
        except OSError as exc:
            digest.update(f"missing:{type(exc).__name__}".encode())
        else:
            if full_path.is_symlink():
                try:
                    digest.update(f"symlink:{os.readlink(full_path)}".encode())
                except OSError as exc:
                    digest.update(f"symlink-error:{type(exc).__name__}".encode())
            elif full_path.is_file():
                digest.update(f"file:{stat_result.st_mode}:{stat_result.st_size}:".encode())
                try:
                    with full_path.open("rb") as fh:
                        while True:
                            chunk = fh.read(64 * 1024)
                            if not chunk:
                                break
                            digest.update(chunk)
                except OSError as exc:
                    digest.update(f"file-error:{type(exc).__name__}".encode())
            else:
                digest.update(f"other:{stat_result.st_mode}".encode())

        try:
            index_entry = _run_git(cwd, "ls-files", "-s", "--", path).stdout
        except subprocess.CalledProcessError:
            index_entry = ""
        digest.update(b"\0index\0")
        digest.update(index_entry.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
    return digest.hexdigest()
