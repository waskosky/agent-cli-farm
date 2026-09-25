#!/usr/bin/env python3
"""Validate, retain, and publish exact farm snapshots under a per-manifest lock."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

HEADER = "name\tdir\tcmd\targs"
PROVIDERS = {"codex": "resume", "claude": "--resume", "gemini": "--resume"}
UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
Row = tuple[str, str, str, str]


def parse_manifest(content: bytes) -> list[Row]:
    try:
        lines = content.decode("utf-8").split("\n")
    except UnicodeDecodeError as exc:
        raise ValueError("Malformed manifest encoding") from exc
    if not lines or lines[0] != HEADER:
        raise ValueError("Malformed manifest header")
    rows = []
    sessions = set()
    for number, line in enumerate(lines[1:], 2):
        if not line and number == len(lines):
            continue
        fields = line.split("\t")
        if len(fields) != 4 or any(ord(c) < 32 and c != "\t" for c in line):
            raise ValueError(f"Malformed manifest row {number}: expected four TSV fields")
        name, directory, command, args = fields
        if not name.strip() or not command.strip():
            raise ValueError(f"Malformed manifest row {number}: name and command are required")
        try:
            parsed_command = command.strip()
            if parsed_command[0] in {"'", '"'} and parsed_command[-1] == parsed_command[0]:
                parsed_command = parsed_command[1:-1].strip()
            tokens = shlex.split(parsed_command)
            provider = Path(tokens[0]).name
            if provider in PROVIDERS:
                arguments = shlex.split(args) if args else tokens[1:]
                if (
                    len(arguments) != 2
                    or arguments[0] != PROVIDERS[provider]
                    or not UUID.fullmatch(arguments[1])
                ):
                    raise ValueError(
                        "provider requires an exact session ID; latest/continue is disabled"
                    )
                session = (provider, arguments[1].lower())
                if session in sessions:
                    raise ValueError("duplicate provider conversation")
                sessions.add(session)
                command, args = provider, f"{arguments[0]} {session[1]}"
        except (ValueError, IndexError) as exc:
            raise ValueError(f"Invalid manifest row {number}: {exc}") from exc
        rows.append((name, directory, command, args))
    if not rows:
        raise ValueError("Empty manifest: no restorable windows; previous snapshot was preserved")
    return rows


def serialize(rows: list[Row]) -> bytes:
    return (HEADER + "\n" + "".join("\t".join(row) + "\n" for row in rows)).encode("utf-8")


def identities(rows: list[Row]) -> Counter:
    return Counter((row[2], row[3]) if row[2] in PROVIDERS else row for row in rows)


def resolve_codex_rows(rows: list[Row]) -> list[Row]:
    helper = os.environ.get(
        "CODEX_SESSION_META_BIN", str(Path(__file__).with_name("codex-session-meta.py"))
    )
    resolved_rows = []
    for name, directory, command, args in rows:
        if command == "codex":
            original_id = args.split()[1]
            result = subprocess.run(
                [helper, "resolve-id", original_id], text=True, capture_output=True, check=False
            )
            session_id = result.stdout.strip()
            if result.returncode != 0 or not UUID.fullmatch(session_id):
                raise ValueError(
                    f"Unable to resolve exact Codex identity for manifest window: {name}"
                )
            if session_id != original_id:
                print(f"Repairing saved child-thread reference for window: {name}", file=sys.stderr)
            args = f"resume {session_id}"
        resolved_rows.append((name, directory, command, args))
    # Different saved child IDs may map to the same resumable conversation.
    return parse_manifest(serialize(resolved_rows))


def sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, content: bytes) -> None:
    descriptor, filename = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def history_directory(manifest: Path) -> Path:
    return manifest.with_name(manifest.name + ".history")


def archive(manifest: Path, content: bytes) -> Path:
    history = history_directory(manifest)
    history.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256(content).hexdigest()
    for existing in history.glob(f"*-{digest}.tsv"):
        if existing.read_bytes() == content:
            return existing
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    snapshot = history / f"{timestamp}-{digest}.tsv"
    atomic_write(snapshot, content)
    return snapshot


@contextmanager
def manifest_lock(manifest: Path):
    manifest.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(manifest.resolve()) + ".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "a") as handle:
        deadline = time.monotonic() + 30
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError(
                        "Another save/restore holds this manifest lock; retry shortly"
                    ) from None
                time.sleep(0.05)
        yield


def publish(candidate: Path, manifest: Path, autosave: bool) -> None:
    rows = parse_manifest(candidate.read_bytes())
    content = serialize(rows)
    previous = manifest.read_bytes() if manifest.exists() else None
    if previous == content:
        print(f"Snapshot unchanged; preserved {manifest}")
        return
    if previous is not None:
        archive(manifest, previous)
    snapshot = archive(manifest, content)
    if autosave and previous is not None:
        previous_rows = parse_manifest(previous)
        if identities(previous_rows) - identities(rows):
            print(f"Autosave preserved {manifest}: current farm omits saved conversations/windows.")
            print(
                f"Current farm archived at {snapshot}; use a manual save to change the restore point."
            )
            return
    atomic_write(manifest, content)
    print(f"Saved {len(rows)} window(s) to {manifest}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("manifest", type=Path)
    validate.add_argument("--resolve-codex", action="store_true")
    save = commands.add_parser("publish")
    save.add_argument("candidate", type=Path)
    save.add_argument("manifest", type=Path)
    save.add_argument("--autosave", action="store_true")
    history = commands.add_parser("history")
    history.add_argument("manifest", type=Path)
    lock = commands.add_parser("lock-run")
    lock.add_argument("manifest")
    lock.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.action == "validate":
            rows = parse_manifest(args.manifest.read_bytes())
            if args.resolve_codex:
                rows = resolve_codex_rows(rows)
            sys.stdout.buffer.write(serialize(rows))
        elif args.action == "history":
            for path in sorted(history_directory(args.manifest).glob("*.tsv"), reverse=True):
                try:
                    summary = f"{len(parse_manifest(path.read_bytes()))} windows"
                except ValueError:
                    summary = "invalid legacy manifest"
                print(f"{summary}\t{path}")
        elif args.action == "lock-run":
            if not args.command:
                raise ValueError("Missing command for manifest lock")
            with manifest_lock(Path(args.manifest)):
                env = {
                    **os.environ,
                    "CODEXFARM_LOCKED_MANIFEST": str(args.manifest),
                    "CODEXFARM_LOCK_PARENT_PID": str(os.getpid()),
                }
                return subprocess.call(args.command, env=env)
        elif Path(
            os.environ.get("CODEXFARM_LOCKED_MANIFEST", "")
        ) == args.manifest and os.environ.get("CODEXFARM_LOCK_SCRIPT_PID") == str(os.getppid()):
            publish(args.candidate, args.manifest, args.autosave)
        else:
            with manifest_lock(args.manifest):
                publish(args.candidate, args.manifest, args.autosave)
    except (OSError, ValueError) as exc:
        print(f"Manifest operation failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
