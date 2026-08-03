#!/usr/bin/env python3
"""Run a secure pager while making a standalone Escape key close it."""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
import time
import tty
from collections.abc import Sequence

ESCAPE = b"\x1b"
ESCAPE_TIMEOUT_SECONDS = 0.15
QUIT_SEQUENCE = b"\x07q"  # Ctrl-G cancels a less prompt; q then closes less.
READ_SIZE = 64 * 1024


def _copy_window_size(source_fd: int, destination_fd: int) -> None:
    try:
        window_size = fcntl.ioctl(source_fd, termios.TIOCGWINSZ, b"\0" * 8)
        rows, columns, _, _ = struct.unpack("HHHH", window_size)
        if rows > 0 and columns > 0:
            fcntl.ioctl(destination_fd, termios.TIOCSWINSZ, window_size)
    except OSError:
        pass


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except InterruptedError:
            continue
        view = view[written:]


def _wait_for_child(pid: int, *, block: bool) -> int | None:
    flags = 0 if block else os.WNOHANG
    try:
        waited_pid, status = os.waitpid(pid, flags)
    except ChildProcessError:
        return 0
    if waited_pid == 0:
        return None
    return os.waitstatus_to_exitcode(status)


def run(command: Sequence[str]) -> int:
    if not command:
        print("deep-history pager: missing pager command", file=sys.stderr)
        return 2
    if not os.isatty(sys.stdin.fileno()) or not os.isatty(sys.stdout.fileno()):
        os.execvpe(command[0], list(command), os.environ)

    terminal_state = termios.tcgetattr(sys.stdin.fileno())
    child_pid, master_fd = pty.fork()
    if child_pid == 0:
        os.execvpe(command[0], list(command), os.environ)

    child_status: int | None = None
    pending_escape_at: float | None = None
    input_open = True
    master_open = True
    resize_pending = True

    def handle_resize(_signum: int, _frame: object) -> None:
        nonlocal resize_pending
        resize_pending = True

    previous_winch = signal.signal(signal.SIGWINCH, handle_resize)
    try:
        tty.setraw(sys.stdin.fileno(), when=termios.TCSANOW)
        while master_open:
            if resize_pending:
                _copy_window_size(sys.stdout.fileno(), master_fd)
                resize_pending = False

            now = time.monotonic()
            timeout = None
            if pending_escape_at is not None:
                timeout = max(0.0, pending_escape_at + ESCAPE_TIMEOUT_SECONDS - now)

            readers = [master_fd]
            if input_open:
                readers.append(sys.stdin.fileno())
            try:
                readable, _, _ = select.select(readers, [], [], timeout)
            except InterruptedError:
                continue

            now = time.monotonic()
            if pending_escape_at is not None and now >= (
                pending_escape_at + ESCAPE_TIMEOUT_SECONDS
            ):
                _write_all(master_fd, QUIT_SEQUENCE)
                pending_escape_at = None

            if input_open and sys.stdin.fileno() in readable:
                try:
                    data = os.read(sys.stdin.fileno(), READ_SIZE)
                except InterruptedError:
                    continue
                if not data:
                    input_open = False
                elif pending_escape_at is not None:
                    combined = ESCAPE + data
                    pending_escape_at = None
                    if combined.endswith(ESCAPE):
                        _write_all(master_fd, combined[:-1])
                        pending_escape_at = time.monotonic()
                    else:
                        _write_all(master_fd, combined)
                elif data.endswith(ESCAPE):
                    _write_all(master_fd, data[:-1])
                    pending_escape_at = time.monotonic()
                else:
                    _write_all(master_fd, data)

            if master_fd in readable:
                try:
                    output = os.read(master_fd, READ_SIZE)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    output = b""
                if output:
                    _write_all(sys.stdout.fileno(), output)
                else:
                    master_open = False

            if child_status is None:
                child_status = _wait_for_child(child_pid, block=False)
    finally:
        signal.signal(signal.SIGWINCH, previous_winch)
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, terminal_state)
        try:
            os.close(master_fd)
        except OSError:
            pass
        if child_status is None:
            child_status = _wait_for_child(child_pid, block=True)

    return child_status or 0


def main(arguments: Sequence[str] | None = None) -> int:
    command = list(sys.argv[1:] if arguments is None else arguments)
    if command[:1] == ["--"]:
        command.pop(0)
    return run(command)


if __name__ == "__main__":
    raise SystemExit(main())
