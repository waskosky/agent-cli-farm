#!/usr/bin/env python3
"""Validate, retain, and publish exact farm snapshots under a per-manifest lock."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
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


def parse_manifest(
    content: bytes, *, empty_commands: list[tuple[int, str]] | None = None
) -> list[Row]:
    """Validate every row; an explicit collector permits omitting empty commands."""
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
        if empty_commands is not None and name.strip() and not command.strip() and not args.strip():
            empty_commands.append((number, name))
            continue
        if not name.strip() or not command.strip():
            raise ValueError(
                f"Malformed manifest row {number}: name and command are required (window {name!r})"
            )
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


def repair_empty_commands(manifest: Path, *, resolve_codex: bool = False) -> list[Row]:
    """Under the manifest lock, retain original bytes before a narrowly scoped repair."""
    previous = manifest.read_bytes()
    removed: list[tuple[int, str]] = []
    rows = parse_manifest(previous, empty_commands=removed)
    if resolve_codex:
        rows = resolve_codex_rows(rows)
    # Finish validation before even creating history. Never turn an empty or
    # ambiguous provider snapshot into an apparently successful recovery.
    if removed:
        content = serialize(rows)
        backup = archive(manifest, previous)
        archive(manifest, content)
        atomic_write(manifest, content)
        print(f"Original manifest preserved at {backup}", file=sys.stderr)
        for number, name in removed:
            print(
                f"Omitted row {number} (window {name!r}): no command or arguments were saved.",
                file=sys.stderr,
            )
        # This is a repair, not a live capture; do not publish a capture receipt.
        print(f"Repaired manifest retains {len(rows)} restorable window(s).", file=sys.stderr)
    return rows


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


def atomic_status(path: Path, value: dict) -> None:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("unsafe manifest status file; existing path preserved")
    except FileNotFoundError:
        pass
    data = (
        json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    if len(data) > 65536:
        raise ValueError("manifest status exceeds size budget")
    atomic_write(path, data)


def prune_history(manifest: Path, keep: int) -> dict:
    if type(keep) is not int or not 1 <= keep <= 4096:
        raise ValueError("history retention must be within 1..4096")
    history = history_directory(manifest)
    if not history.exists():
        return {"kept": 0, "pruned": 0, "operator_files_preserved": 0}
    info = history.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("history directory must remain private, owned and not a symlink")
    candidates = []
    preserved = 0
    pattern = re.compile(r"[0-9]{8}T[0-9]{6}\.[0-9]{6}Z-([a-f0-9]{64})\.tsv\Z")
    for index, path in enumerate(history.iterdir()):
        if index >= 4096:
            raise ValueError("history scan exceeds its bounded budget; no snapshots were pruned")
        match = pattern.fullmatch(path.name)
        if not match:
            preserved += 1
            continue
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > 4194304
        ):
            preserved += 1
            continue
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != match[1]:
            preserved += 1
            continue
        candidates.append(
            (
                path.name,
                path,
                match[1],
                info.st_dev,
                info.st_ino,
                info.st_mtime_ns,
                info.st_ctime_ns,
                info.st_size,
            )
        )
    current = hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest.is_file() else None
    candidates.sort(reverse=True)
    pruned = 0
    for _name, path, digest, device, inode, modified, changed, size in candidates[keep:]:
        if digest == current:
            continue
        info = path.lstat()
        if (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size) != (
            device,
            inode,
            modified,
            changed,
            size,
        ):
            preserved += 1
            continue
        path.unlink()
        pruned += 1
    sync_directory(history)
    return {
        "kept": len(candidates) - pruned,
        "pruned": pruned,
        "operator_files_preserved": preserved,
    }


def record_capture(manifest: Path, candidate_rows: int, unresolved: int) -> None:
    content = manifest.read_bytes()
    atomic_status(
        manifest.with_name(manifest.name + ".capture.json"),
        {
            "schema_version": 1,
            "checked_at": time.time(),
            "candidate_rows": candidate_rows,
            "recovery_rows": len(parse_manifest(content)),
            "unresolved_panes": unresolved,
            "complete": unresolved == 0,
            "manifest_sha256": hashlib.sha256(content).hexdigest(),
        },
    )
    choice = os.environ.get("CODEXFARM_MANIFEST_HISTORY_KEEP", "")
    if choice:
        prune_history(manifest, int(choice))


def capture_check(manifest: Path) -> None:
    path = manifest.with_name(manifest.name + ".capture.json")
    with path.open("rb") as source:
        data = source.read(65537)
    if len(data) > 65536:
        raise ValueError("manifest capture receipt exceeds size budget")
    value = json.loads(data)
    checked = value.get("checked_at") if isinstance(value, dict) else None
    if (
        not isinstance(checked, int | float)
        or isinstance(checked, bool)
        or not -5 <= time.time() - checked <= 900
    ):
        raise ValueError("manifest capture heartbeat is missing or older than 15 minutes")
    if value.get("schema_version") != 1 or value.get("complete") is not True:
        raise ValueError("manifest capture has unresolved pane coverage")
    if hashlib.sha256(manifest.read_bytes()).hexdigest() != value.get("manifest_sha256"):
        raise ValueError("manifest changed since its last verified capture")
    print("Manifest capture is recent and complete")


RESTORE_OUTCOMES = frozenset(
    {
        "launched",
        "already_present",
        "active_elsewhere",
        "inspection_failed",
        "identity_conflict",
        "writer_conflict",
        "launch_failed",
        "memory_pressure",
    }
)


def record_restore(manifest: Path, normalized: Path, outcomes: Path, exit_code: int) -> None:
    content = manifest.read_bytes()
    rows = parse_manifest(normalized.read_bytes())
    with outcomes.open("rb") as source:
        raw = source.read(1048577)
    if len(raw) > 1048576:
        raise ValueError("restore outcome log exceeds size budget")
    entries = []
    seen = set()
    counts = Counter()
    for line in raw.decode().splitlines():
        index, outcome = line.split("\t")
        number = int(index)
        if not 1 <= number <= len(rows) or number in seen or outcome not in RESTORE_OUTCOMES:
            raise ValueError("invalid restore outcome entry")
        seen.add(number)
        counts[outcome] += 1
        if len(entries) < 256:
            identity = hashlib.sha256("\t".join(rows[number - 1][2:]).encode()).hexdigest()
            entries.append({"row": number, "identity_sha256": identity, "outcome": outcome})
    path = manifest.with_name(manifest.name + ".restore.json")
    digest = hashlib.sha256(content).hexdigest()
    previous = {}
    try:
        with path.open("rb") as source:
            prior = source.read(65537)
        if len(prior) > 65536:
            raise ValueError("previous restore receipt exceeds size budget")
        previous = json.loads(prior)
        if not isinstance(previous, dict) or previous.get("manifest_sha256") != digest:
            previous = {}
    except FileNotFoundError:
        pass
    attempt = previous.get("attempt", 0)
    if type(attempt) is not int or not 0 <= attempt <= 1000000:
        raise ValueError("invalid previous restore attempt")
    stamp = time.time()
    atomic_status(
        path,
        {
            "schema_version": 1,
            "checked_at": stamp,
            "first_attempt_at": previous.get("first_attempt_at", stamp),
            "attempt": attempt + 1,
            "manifest_sha256": digest,
            "exit_code": exit_code,
            "complete": exit_code == 0 and len(seen) == len(rows),
            "expected_rows": len(rows),
            "outcomes_count": len(seen),
            "counts": dict(counts),
            "outcomes_truncated": len(seen) > len(entries),
            "rows": entries,
        },
    )


def publish(
    candidate: Path, manifest: Path, autosave: bool, merge: bool = False, unresolved: int = 0
) -> None:
    choice = os.environ.get("CODEXFARM_MANIFEST_HISTORY_KEEP", "")
    if choice and (not choice.isdigit() or not 1 <= int(choice) <= 4096):
        raise ValueError("CODEXFARM_MANIFEST_HISTORY_KEEP must be within 1..4096")
    rows = parse_manifest(candidate.read_bytes())
    candidate_rows = len(rows)
    previous = manifest.read_bytes() if manifest.exists() else None
    if merge and previous is not None:
        try:
            previous_rows = parse_manifest(previous)
        except ValueError:
            # Retain malformed legacy bytes in history before publishing verified data.
            previous_rows = []
        missing = identities(previous_rows) - identities(rows)
        for row in previous_rows:
            identity = next(iter(identities([row])))
            if missing[identity]:
                rows.append(row)
                missing[identity] -= 1
        rows = parse_manifest(serialize(rows))
    content = serialize(rows)
    if previous == content:
        record_capture(manifest, candidate_rows, unresolved)
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
            record_capture(manifest, candidate_rows, unresolved)
            return
    atomic_write(manifest, content)
    record_capture(manifest, candidate_rows, unresolved)
    print(f"Saved {len(rows)} window(s) to {manifest}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("manifest", type=Path)
    validate.add_argument("--resolve-codex", action="store_true")
    validate.add_argument(
        "--repair-empty-commands",
        action="store_true",
        help="back up and remove only named rows with both command and arguments empty",
    )
    save = commands.add_parser("publish")
    save.add_argument("candidate", type=Path)
    save.add_argument("manifest", type=Path)
    save.add_argument("--autosave", action="store_true")
    save.add_argument("--merge", action="store_true")
    save.add_argument("--unresolved", type=int, default=0)
    check = commands.add_parser("capture-check")
    check.add_argument("manifest", type=Path)
    restore = commands.add_parser("record-restore")
    restore.add_argument("manifest", type=Path)
    restore.add_argument("--normalized", type=Path, required=True)
    restore.add_argument("--outcomes", type=Path, required=True)
    restore.add_argument("--exit-code", type=int, required=True)
    history = commands.add_parser("history")
    history.add_argument("manifest", type=Path)
    prune = commands.add_parser("prune-history")
    prune.add_argument("manifest", type=Path)
    prune.add_argument("--keep", type=int, required=True)
    lock = commands.add_parser("lock-run")
    lock.add_argument("manifest")
    lock.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == "publish" and args.autosave and args.merge:
        parser.error("--merge cannot be combined with --autosave")
    if args.action == "publish" and not 0 <= args.unresolved <= 1000000:
        parser.error("unresolved count must be within 0..1000000")
    if args.action == "prune-history" and not 1 <= args.keep <= 4096:
        parser.error("history retention must be within 1..4096")
    if args.action == "record-restore" and not 0 <= args.exit_code <= 255:
        parser.error("restore exit code must be within 0..255")
    try:
        if args.action == "prune-history":
            with manifest_lock(args.manifest):
                print(json.dumps(prune_history(args.manifest, args.keep), sort_keys=True))
        elif args.action == "capture-check":
            capture_check(args.manifest)
        elif args.action == "record-restore":
            if Path(
                os.environ.get("CODEXFARM_LOCKED_MANIFEST", "")
            ) == args.manifest and os.environ.get("CODEXFARM_LOCK_SCRIPT_PID") == str(os.getppid()):
                record_restore(args.manifest, args.normalized, args.outcomes, args.exit_code)
            else:
                with manifest_lock(args.manifest):
                    record_restore(args.manifest, args.normalized, args.outcomes, args.exit_code)
        elif args.action == "validate":
            if args.repair_empty_commands:
                if Path(
                    os.environ.get("CODEXFARM_LOCKED_MANIFEST", "")
                ) == args.manifest and os.environ.get("CODEXFARM_LOCK_SCRIPT_PID") == str(
                    os.getppid()
                ):
                    rows = repair_empty_commands(args.manifest, resolve_codex=args.resolve_codex)
                else:
                    with manifest_lock(args.manifest):
                        rows = repair_empty_commands(
                            args.manifest, resolve_codex=args.resolve_codex
                        )
            else:
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
            publish(args.candidate, args.manifest, args.autosave, args.merge, args.unresolved)
        else:
            with manifest_lock(args.manifest):
                publish(args.candidate, args.manifest, args.autosave, args.merge, args.unresolved)
    except (OSError, ValueError) as exc:
        print(f"Manifest operation failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
