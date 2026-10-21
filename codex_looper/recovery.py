"""Explicit verification, private extraction and copies of recovery archives.

No operation replaces a live provider home, executes archive content, enables
archives, uploads to a service, or resumes an interrupted command.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import selectors
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath

from .health import read_bounded, state_directory
from .resource_config import private_directory, read_private_json, write_private_json

MAX_BYTES = 512 * 1024 * 1024
MAX_MEMBERS = 20000
MAX_INVENTORY_BYTES = 8 * 1024 * 1024
DISK_RESERVE = 1024 * 1024 * 1024
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
SECRET_PARTS = frozenset(
    {".git", ".ssh", ".aws", ".gnupg", ".codex", ".claude", ".gemini", "node_modules", ".venv"}
)
SECRET_NAMES = frozenset(
    {".env", "auth.json", "credentials", "credentials.json", "id_rsa", "id_ed25519"}
)


def safe_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or ":" in name
        or any(ord(c) < 32 or ord(c) == 127 for c in name)
        or any(part in ("", ".", "..") for part in name.rstrip("/").split("/"))
    ):
        raise ValueError("archive contains an unsafe relative path")
    return path


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sha256(stream) -> str:
    digest = hashlib.sha256()
    while data := stream.read(1024 * 1024):
        digest.update(data)
    return digest.hexdigest()


def expected_digest(archive: Path, supplied: str | None = None) -> str:
    if supplied is not None:
        if not DIGEST.fullmatch(supplied):
            raise ValueError("sha256 must contain exactly 64 lowercase hexadecimal characters")
        return supplied
    for receipt in (
        archive.with_name(archive.name + ".integrity.json"),
        archive.parent / "latest.json",
    ):
        try:
            raw = read_bounded(receipt)
            value = json.loads(raw)
            if (
                isinstance(value, dict)
                and value.get("archive") == archive.name
                and isinstance(value.get("sha256"), str)
                and DIGEST.fullmatch(value["sha256"])
            ):
                return value["sha256"]
        except FileNotFoundError:
            continue
    raise ValueError("no matching checksum receipt; supply a trusted --sha256 value")


def source_stream(path: Path, max_bytes: int):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise ValueError("archive is not a regular file within the size budget")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def file_generation(stream) -> tuple:
    return stat_generation(os.fstat(stream.fileno()))


def stat_generation(value) -> tuple:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def walk_archive(stream, *, max_bytes: int, deadline: float, target: Path | None = None) -> dict:
    stream.seek(0)
    files = {}
    directories = set()
    total = 0
    metadata = None
    count = 0
    with tarfile.open(fileobj=stream, mode="r|*") as bundle:
        for member in bundle:
            count += 1
            if count > MAX_MEMBERS or time.monotonic() > deadline:
                raise ValueError("archive exceeds its member/time budget")
            relative = safe_name(member.name)
            name = str(relative)
            if name in files or name in directories:
                raise ValueError("archive contains duplicate members")
            if member.isdir():
                directories.add(name)
                continue
            if not member.isfile() or member.size < 0:
                raise ValueError("archive links and special files are not accepted")
            total += member.size
            if total > max_bytes:
                raise ValueError("archive exceeds its uncompressed size budget")
            files[name] = member.size
            if name == "snapshot.json" and member.size > MAX_INVENTORY_BYTES:
                raise ValueError("archive inventory exceeds size budget")
            source = bundle.extractfile(member)
            if source is None:
                raise ValueError("archive member is unreadable")
            destination = None
            captured = bytearray()
            try:
                if target is not None:
                    path = target.joinpath(*relative.parts)
                    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    destination = os.fdopen(fd, "wb")
                remaining = member.size
                while remaining:
                    if time.monotonic() > deadline:
                        raise ValueError("archive verification time budget exceeded")
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("truncated archive member")
                    remaining -= len(chunk)
                    if name == "snapshot.json":
                        captured.extend(chunk)
                    if destination:
                        destination.write(chunk)
                if destination:
                    destination.flush()
                    os.fsync(destination.fileno())
                if name == "snapshot.json":
                    metadata = json.loads(captured)
            finally:
                source.close()
                if destination:
                    destination.close()
    if (
        not isinstance(metadata, dict)
        or metadata.get("schema") != 1
        or not isinstance(metadata.get("files"), list)
    ):
        raise ValueError("archive has no compatible inventory")
    inventory = {}
    if len(metadata["files"]) > MAX_MEMBERS:
        raise ValueError("archive inventory exceeds member budget")
    for item in metadata["files"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("path"), str)
            or type(item.get("bytes")) is not int
            or item["bytes"] < 0
        ):
            raise ValueError("archive inventory is invalid")
        name = str(safe_name(item["path"]))
        if name in inventory:
            raise ValueError("archive inventory contains duplicates")
        inventory[name] = item["bytes"]
    actual = {name: size for name, size in files.items() if name != "snapshot.json"}
    if actual != inventory:
        raise ValueError("archive contents do not match the inventory")
    saved = metadata.get("manifest_save_ok")
    if saved is not None and type(saved) is not bool:
        raise ValueError("archive inventory save status is invalid")
    return {
        "files": len(actual),
        "uncompressed_bytes": total,
        "manifest_save_ok": saved,
    }


def check_databases(root: Path, deadline: float) -> int:
    count = 0
    for path in (root / "codex").glob("*.sqlite"):
        if time.monotonic() > deadline:
            raise ValueError("database verification time budget exceeded")
        connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=2)
        try:
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            if connection.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
                raise ValueError("archive database integrity check failed")
            count += 1
        finally:
            connection.close()
    return count


def verify_archive(
    archive: Path,
    *,
    expected: str | None = None,
    destination: Path | None = None,
    max_bytes: int = MAX_BYTES,
) -> dict:
    """Verify checksum, bounded inventory and databases; extract only into a new private tree."""
    archive = Path(archive).absolute()
    digest = expected_digest(archive, expected)
    deadline = time.monotonic() + 60
    created = None
    generation = None
    try:
        with source_stream(archive, max_bytes) as source:
            before = file_generation(source)
            if sha256(source) != digest:
                raise ValueError("archive checksum mismatch")
            summary = walk_archive(source, max_bytes=max_bytes, deadline=deadline)
            if destination is None:
                context = tempfile.TemporaryDirectory(prefix="codexfarm-verify-")
                target = Path(context.name)
                target.chmod(0o700)
            else:
                context = None
                target = Path(destination).absolute()
                # Never replace an existing tree, including an empty directory or symlink.
                target.mkdir(mode=0o700)
                created = target
                info = target.lstat()
                generation = info.st_dev, info.st_ino
            try:
                if shutil.disk_usage(target).free < summary["uncompressed_bytes"] + DISK_RESERVE:
                    raise ValueError("insufficient disk space for a private verification copy")
                walk_archive(source, max_bytes=max_bytes, deadline=deadline, target=target)
                summary["databases_checked"] = check_databases(target, deadline)
                if file_generation(source) != before:
                    raise ValueError("archive changed during verification")
                # Flush extraction directories from leaves to root before reporting success.
                for directory, _, _ in os.walk(target, topdown=False):
                    sync_directory(Path(directory))
                if created:
                    sync_directory(created.parent)
            finally:
                if context:
                    context.cleanup()
        return {"schema_version": 1, "verified": True, "sha256": digest, **summary}
    except BaseException:
        if created:
            info = created.lstat()
            if (info.st_dev, info.st_ino) == generation:
                shutil.rmtree(created)
        raise


def copy_archive(
    archive: Path, destination: Path, *, max_bytes: int = MAX_BYTES, keep: int = 3
) -> dict:
    """Copy only after verification, flush and verify the published destination bytes."""
    archive = Path(archive).absolute()
    verified = verify_archive(archive, max_bytes=max_bytes)
    destination = Path(destination).absolute()
    if type(keep) is not int or not 1 <= keep <= 100:
        raise ValueError("copy retention must be within 1..100")
    private_directory(destination, create=True)
    if destination.resolve() == archive.parent.resolve():
        raise ValueError("copy destination must differ from the source archive directory")
    safe_name(archive.name)
    index_path = destination / "copy-index.json"
    try:
        index = read_private_json(index_path)
    except FileNotFoundError:
        index = {"schema_version": 1, "copies": []}
    copies = index.get("copies")
    if index.get("schema_version") != 1 or not isinstance(copies, list) or len(copies) >= 128:
        raise ValueError("copy retention index is invalid or full; existing files preserved")
    for entry in copies:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("archive"), str)
            or len(PurePosixPath(entry["archive"]).parts) != 1
            or not DIGEST.fullmatch(str(entry.get("sha256", "")))
        ):
            raise ValueError("copy retention index contains invalid entries")
        safe_name(entry["archive"])
    target = destination / archive.name
    receipt = target.with_name(target.name + ".integrity.json")
    with source_stream(archive, max_bytes) as source:
        before = file_generation(source)
        expected_receipt = {
            "schema_version": 1,
            "archive": target.name,
            "sha256": verified["sha256"],
            "bytes": before[2],
        }
        receipt_exists = receipt.exists() or receipt.is_symlink()
        if receipt_exists and read_private_json(receipt) != expected_receipt:
            raise ValueError("destination checksum receipt was modified; existing files preserved")
        if target.exists() or target.is_symlink():
            with source_stream(target, max_bytes) as existing:
                if sha256(existing) != verified["sha256"]:
                    raise ValueError("destination archive already exists with different bytes")
        else:
            if shutil.disk_usage(destination).free < before[2] + DISK_RESERVE:
                raise ValueError("insufficient disk space for a safe archive copy")
            fd, temporary = tempfile.mkstemp(prefix=".copy-", dir=destination)
            try:
                with os.fdopen(fd, "wb") as output:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                with source_stream(Path(temporary), max_bytes) as output:
                    if sha256(output) != verified["sha256"] or file_generation(source) != before:
                        raise ValueError("source archive changed while copying")
                # Link publishes exclusively; never overwrite an operator file after a race.
                os.link(temporary, target, follow_symlinks=False)
                sync_directory(destination)
            finally:
                Path(temporary).unlink(missing_ok=True)
        if not receipt_exists:
            write_private_json(receipt, expected_receipt)
    copies = [entry for entry in copies if entry["archive"] != target.name]
    copies.append({"archive": target.name, "sha256": verified["sha256"], "created_at": time.time()})
    write_private_json(index_path, {"schema_version": 1, "copies": copies})
    retained = []
    preserved = 0
    for entry in copies[:-keep]:
        old = destination / entry["archive"]
        old_receipt = old.with_name(old.name + ".integrity.json")
        try:
            with source_stream(old, max_bytes) as stream:
                generation = file_generation(stream)
                if sha256(stream) != entry["sha256"] or file_generation(stream) != generation:
                    retained.append(entry)
                    preserved += 1
                    continue
                receipt_generation = stat_generation(old_receipt.lstat())
                if read_private_json(old_receipt) != {
                    "schema_version": 1,
                    "archive": old.name,
                    "sha256": entry["sha256"],
                    "bytes": generation[2],
                }:
                    retained.append(entry)
                    preserved += 1
                    continue
                current = old.lstat()
                if (
                    stat_generation(current) != generation
                    or stat_generation(old_receipt.lstat()) != receipt_generation
                ):
                    retained.append(entry)
                    preserved += 1
                    continue
                old.unlink()
            old_receipt.unlink(missing_ok=True)
        except FileNotFoundError:
            if old.exists() or old.is_symlink():
                retained.append(entry)
                preserved += 1
            continue
        except (OSError, ValueError):
            retained.append(entry)
            preserved += 1
    retained.extend(copies[-keep:])
    write_private_json(index_path, {"schema_version": 1, "copies": retained})
    sync_directory(destination)
    return {
        **verified,
        "copy_verified": True,
        "retained_copies": len(retained),
        "preserved_operator_files": preserved,
        "different_filesystem": archive.stat().st_dev != target.stat().st_dev,
        "encryption": "destination/operator managed",
    }


def bounded_git(repo: Path, arguments: list[str]) -> bytes:
    command = [
        "git",
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-C",
        str(repo),
        *arguments,
    ]
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
    )
    output = bytearray()
    deadline = time.monotonic() + 5
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, True)
            selector.register(process.stderr, selectors.EVENT_READ, False)
            while selector.get_map():
                if time.monotonic() > deadline:
                    raise ValueError("workspace git listing timed out")
                for key, _ in selector.select(0.1):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                    elif key.data:
                        output.extend(data)
                        if len(output) > 2 * 1024 * 1024:
                            raise ValueError("workspace file listing exceeds size limit")
            if process.wait(timeout=1) != 0:
                raise ValueError("workspace must be a readable Git repository")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        process.stdout.close()
        process.stderr.close()
    return bytes(output)


def open_workspace_file(root: Path, relative: PurePosixPath):
    """Hold directory descriptors so symlink swaps cannot escape the selected root."""
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_fd
        fd = os.open(
            relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError("workspace selection includes a special file")
        return os.fdopen(fd, "rb")
    finally:
        os.close(directory)


def workspace_snapshot(
    repo: Path, destination: Path, *, include_untracked=(), max_bytes=MAX_BYTES
) -> dict:
    """Explicit workspace snapshot: tracked work plus exact selected untracked files."""
    repo = Path(repo).absolute()
    destination = Path(destination).absolute()
    root = bounded_git(repo, ["rev-parse", "--show-toplevel"]).decode().strip()
    if Path(root).resolve() != repo.resolve() or repo.is_symlink():
        raise ValueError("workspace must name the exact repository root, without a symlink")
    names = {
        os.fsdecode(name)
        for name in bounded_git(repo, ["ls-files", "--cached", "-z"]).split(b"\0")
        if name
    }
    names.update(include_untracked)
    if len(names) > MAX_MEMBERS - 1:
        raise ValueError("workspace has too many selected files")
    selected = []
    excluded = 0
    total = 0
    for name in sorted(names):
        relative = safe_name(name)
        if (
            SECRET_PARTS.intersection(relative.parts)
            or relative.name in SECRET_NAMES
            or relative.name.startswith(".env.")
            or relative.suffix.lower() in (".pem", ".key", ".p12", ".pfx")
        ):
            excluded += 1
            continue
        try:
            with open_workspace_file(repo, relative) as source:
                size = os.fstat(source.fileno()).st_size
        except FileNotFoundError:
            # Tracked deletions are represented by absence from this whole-tree snapshot.
            if name in include_untracked:
                raise ValueError("selected untracked workspace file is missing") from None
            continue
        total += size
        if total > max_bytes:
            raise ValueError("workspace exceeds its source size budget")
        selected.append(relative)
    if not selected:
        raise ValueError("workspace contains no selected regular files")
    private_directory(destination.parent, create=True)
    if destination.exists() or destination.is_symlink():
        raise ValueError("workspace snapshot destination already exists")
    if shutil.disk_usage(destination.parent).free < total + DISK_RESERVE:
        raise ValueError("insufficient disk space for a workspace snapshot")
    fd, temporary = tempfile.mkstemp(prefix=".workspace-", dir=destination.parent)
    inventory = []
    source_bytes = 0
    deadline = time.monotonic() + 60
    try:
        with os.fdopen(fd, "wb") as output:
            with tarfile.open(fileobj=output, mode="w:gz", compresslevel=1) as bundle:
                for relative in selected:
                    if time.monotonic() > deadline:
                        raise ValueError("workspace snapshot time budget exceeded")
                    with open_workspace_file(repo, relative) as source:
                        before = file_generation(source)
                        source_bytes += before[2]
                        if source_bytes > max_bytes:
                            raise ValueError("workspace grew beyond its size budget")
                        name = "workspaces/repository/" + str(relative)
                        member = bundle.gettarinfo(fileobj=source, arcname=name)
                        member.mode = 0o600
                        member.uid = member.gid = 0
                        member.uname = member.gname = ""
                        bundle.addfile(member, source)
                        if file_generation(source) != before:
                            raise ValueError("workspace file changed during snapshot; retry")
                        inventory.append({"path": name, "bytes": member.size})
                raw = json.dumps(
                    {
                        "schema": 1,
                        "created_at": time.time(),
                        "kind": "workspace",
                        "files": inventory,
                        "excluded_files": excluded,
                    }
                ).encode()
                if len(raw) > MAX_INVENTORY_BYTES:
                    raise ValueError("workspace inventory exceeds size budget")
                member = tarfile.TarInfo("snapshot.json")
                member.size, member.mode = len(raw), 0o600
                bundle.addfile(member, io.BytesIO(raw))
            output.flush()
            os.fsync(output.fileno())
        with source_stream(Path(temporary), max_bytes) as source:
            digest = sha256(source)
        verify_archive(Path(temporary), expected=digest, max_bytes=max_bytes)
        os.link(temporary, destination, follow_symlinks=False)
        sync_directory(destination.parent)
        write_private_json(
            destination.with_name(destination.name + ".integrity.json"),
            {
                "schema_version": 1,
                "archive": destination.name,
                "sha256": digest,
                "bytes": destination.stat().st_size,
            },
        )
        return {
            "schema_version": 1,
            "verified": True,
            "sha256": digest,
            "files": len(inventory),
            "excluded_files": excluded,
            "source_bytes": source_bytes,
        }
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv=None) -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("verify", "extract", "copy"):
        command = commands.add_parser(action)
        command.add_argument("archive", type=Path)
        command.add_argument("--max-mib", type=int, default=512)
        if action in ("verify", "extract"):
            command.add_argument("--sha256")
        if action in ("extract", "copy"):
            command.add_argument("--destination", type=Path, required=True)
        if action == "copy":
            command.add_argument(
                "--keep",
                type=int,
                default=3,
                help="retain this command's verified copies; default 3",
            )
    workspace = commands.add_parser("workspace")
    workspace.add_argument("--repo", type=Path, required=True)
    workspace.add_argument("--destination", type=Path, required=True)
    workspace.add_argument("--include-untracked", action="append", default=[])
    workspace.add_argument("--max-mib", type=int, default=512)
    args = parser.parse_args(argv)
    if not 1 <= args.max_mib <= 16384:
        parser.error("max-mib must be within 1..16384")
    try:
        if args.action == "workspace":
            result = workspace_snapshot(
                args.repo,
                args.destination,
                include_untracked=args.include_untracked,
                max_bytes=args.max_mib * 1024 * 1024,
            )
        elif args.action == "copy":
            result = copy_archive(
                args.archive, args.destination, max_bytes=args.max_mib * 1024 * 1024, keep=args.keep
            )
        else:
            result = verify_archive(
                args.archive,
                expected=args.sha256,
                destination=args.destination if args.action == "extract" else None,
                max_bytes=args.max_mib * 1024 * 1024,
            )
        state = state_directory()
        private_directory(state, create=True)
        write_private_json(
            state / "recovery-status.json",
            {**result, "checked_at": time.time(), "action": args.action},
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (
        OSError,
        ValueError,
        TypeError,
        sqlite3.Error,
        tarfile.TarError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"Recovery operation failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1
