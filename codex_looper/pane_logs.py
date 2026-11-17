"""Bound legacy terminal diagnostic logs without touching provider conversations."""

import argparse
import fcntl
import os
import re
import stat
import sys
import time
from pathlib import Path

LIMIT = 16 * 1024**2
TAIL = 4 * 1024**2


def checked(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise ValueError("log must be an owned regular file")
    return info


def write_stream(path, source, *, limit=LIMIT, tail=TAIL):
    if not 0 < tail < limit - 128:
        raise ValueError("log tail must fit inside its byte budget")
    path = Path(path).absolute()
    if path.parent.resolve() != path.parent:
        raise ValueError("log directory must not contain symlinks")
    checked(path)
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(fd, "r+b", buffering=0) as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        info = os.fstat(stream.fileno())
        current = checked(path)
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("log identity changed")
        while True:
            chunk = source.read(min(65536, limit - tail - 128))
            if not chunk:
                break
            stream.seek(0, os.SEEK_END)
            if stream.tell() + len(chunk) > limit:
                stream.seek(max(0, stream.tell() - tail))
                retained = stream.read(tail)
                stream.seek(0)
                stream.truncate()
                stream.write(b"[farm diagnostic log: older terminal output retired]\n" + retained)
            stream.seek(0, os.SEEK_END)
            stream.write(chunk)


def active_log_paths():
    active, unknown = set(), False
    if not Path("/proc").is_dir():
        unknown = True
    else:
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                if proc.stat().st_uid != os.getuid():
                    continue
                for fd in (proc / "fd").iterdir():
                    try:
                        active.add(os.readlink(fd))
                    except FileNotFoundError:
                        pass
            except FileNotFoundError:
                pass
            except OSError:
                unknown = True
    return active, unknown


def prune_logs(root, *, apply=False, days=30, max_bytes=1024**3):
    root = Path(root).absolute()
    if root.resolve() != root:
        raise ValueError("log root must not contain symlinks")
    report = {"dry_run": not apply, "removed": [], "candidates": [], "over_budget": False}
    if not root.exists():
        return report
    if not 1 <= days <= 365 or type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("invalid diagnostic log retention")
    active, unknown = active_log_paths()
    rows = []
    for path in root.iterdir():
        if not re.fullmatch(r".+_[0-9]{8}-[0-9]{6}(?:_[A-Za-z0-9]+)?\.log", path.name):
            continue
        try:
            info = checked(path)
        except (ValueError, OSError):
            continue
        rows.append((info.st_mtime, path, info))
    total = sum(info.st_size for _, _, info in rows)
    cutoff = time.time() - days * 86400
    for modified, path, info in sorted(rows):
        if unknown or str(path) in active or modified > cutoff:
            continue
        # Expire old terminal mirrors even when below the aggregate byte target.
        report["candidates"].append(str(path))
        if apply:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                current = checked(path)
                if (current.st_ino, current.st_size, current.st_mtime_ns) != (
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                ):
                    continue
                path.unlink()
                report["removed"].append(str(path))
            except BlockingIOError:
                continue
            finally:
                os.close(fd)
        total -= info.st_size
    report.update(
        remaining_bytes=total, over_budget=total > max_bytes, process_scan_incomplete=unknown
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, required=True)
    args = parser.parse_args()
    write_stream(args.path, sys.stdin.buffer)
