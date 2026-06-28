from __future__ import annotations

import asyncio
import os
import re
import signal
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from .models import STREAM_READ_CHUNK_BYTES, ConfigError, ProcessResult
from .retry import parse_output_line

TerminateProcessGroup = Callable[[asyncio.subprocess.Process], Awaitable[None]]
CloseSubprocessTransport = Callable[[asyncio.subprocess.Process], Awaitable[None]]
ProcessStartedCallback = Callable[[int, int | None], None]


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _safe_stream_write(stream: TextIO, text: str) -> None:
    try:
        stream.write(text)
        stream.flush()
    except BrokenPipeError:
        pass


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    except Exception:
        process.terminate()

    try:
        await asyncio.wait_for(process.wait(), timeout=10)
        return
    except asyncio.TimeoutError:
        pass

    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        return
    except Exception:
        process.kill()
    await process.wait()


async def _close_subprocess_transport(process: asyncio.subprocess.Process) -> None:
    transport = getattr(process, "_transport", None)
    close = getattr(transport, "close", None)
    if not callable(close):
        return
    close()
    await asyncio.sleep(0)


async def run_command(
    *,
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
    log_path: Path,
    agent_kind: str,
    patterns: list[re.Pattern[str]],
    scan_stdout: bool,
    kill_on_stop_pattern: bool,
    completion_pattern: re.Pattern[str] | None = None,
    stream_output: bool = True,
    terminate_process_group: TerminateProcessGroup | None = None,
    close_subprocess_transport: CloseSubprocessTransport | None = None,
    utc_stamp_fn: Callable[[], str] | None = None,
    on_process_started: ProcessStartedCallback | None = None,
) -> ProcessResult:
    terminate_process_group = terminate_process_group or _terminate_process_group
    close_subprocess_transport = close_subprocess_transport or _close_subprocess_transport
    stamp = utc_stamp_fn or utc_stamp

    log_path.parent.mkdir(parents=True, exist_ok=True)
    merged_env = os.environ.copy()
    merged_env.update(env)

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            env=merged_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        )
    except FileNotFoundError as exc:
        raise ConfigError(f"executable not found: {command[0]}") from exc
    except PermissionError as exc:
        raise ConfigError(f"executable is not runnable: {command[0]}") from exc

    if on_process_started is not None:
        pgid = None
        if os.name == "posix":
            try:
                pgid = os.getpgid(process.pid)
            except ProcessLookupError:
                pgid = None
        on_process_started(process.pid, pgid)

    result = ProcessResult(returncode=None)
    stop_event = asyncio.Event()

    async def read_stream(reader: asyncio.StreamReader | None, stream_name: str) -> None:
        if reader is None:
            return
        output_stream = sys.stdout if stream_name == "stdout" else sys.stderr

        def handle_line(raw_line: bytes, log_file: TextIO) -> None:
            text = raw_line.decode("utf-8", errors="replace")
            result.output_bytes += len(raw_line)
            if stream_output:
                _safe_stream_write(output_stream, text)
            log_file.write(f"[{stamp()}] {stream_name}: {text}")
            log_file.flush()

            if completion_pattern and completion_pattern.search(text):
                result.completion_detected = True

            parsed = parse_output_line(
                line=text,
                stream=stream_name,
                agent_kind=agent_kind,
                patterns=patterns,
                scan_stdout=scan_stdout,
            )
            if parsed.session_id and not result.session_id:
                result.session_id = parsed.session_id
            if parsed.stop_reason and not result.stop_reason:
                result.stop_reason = parsed.stop_reason
                result.retry_after_seconds = parsed.retry_after_seconds
                result.retry_kind = parsed.retry_kind
                stop_event.set()

        with log_path.open("a", encoding="utf-8") as log_file:
            pending = bytearray()
            try:
                while True:
                    chunk = await reader.read(STREAM_READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    pending.extend(chunk)
                    while True:
                        newline_index = pending.find(b"\n")
                        if newline_index < 0:
                            break
                        raw_line = bytes(pending[: newline_index + 1])
                        del pending[: newline_index + 1]
                        handle_line(raw_line, log_file)
                if pending:
                    handle_line(bytes(pending), log_file)
            except Exception as exc:
                message = f"local {stream_name} reader failed: {type(exc).__name__}: {exc}"
                if not result.stop_reason:
                    result.stop_reason = message
                log_file.write(f"[{stamp()}] {stream_name}_reader_error: {message}\n")
                log_file.flush()
                stop_event.set()
                await terminate_process_group(process)

    async def wait_for_stop() -> None:
        await stop_event.wait()
        if kill_on_stop_pattern:
            await terminate_process_group(process)

    stdout_task = asyncio.create_task(read_stream(process.stdout, "stdout"))
    stderr_task = asyncio.create_task(read_stream(process.stderr, "stderr"))
    stop_task = asyncio.create_task(wait_for_stop())

    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        result.timed_out = True
        if not result.stop_reason:
            result.stop_reason = f"local timeout after {timeout_seconds:g} seconds"
        await terminate_process_group(process)
    finally:
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        if not stop_task.done():
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
        await close_subprocess_transport(process)

    result.returncode = process.returncode
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"[{stamp()}] returncode: {result.returncode}\n")
        if result.stop_reason:
            log_file.write(f"[{stamp()}] stop_reason: {result.stop_reason}\n")
    return result
