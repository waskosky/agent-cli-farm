#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from string import Formatter
from typing import Any, Literal, TextIO

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 only.
    tomllib = None  # type: ignore[assignment]


VERSION = "0.2.0"
AgentKind = Literal["claude", "codex", "generic"]
LooperMode = Literal["single", "sequence"]
TMUX_STATE_OPTION = "@codex_state"
TMUX_STOP_REASON_OPTION = "@codex_stop_reason"
DEFAULT_TIMEOUT_SECONDS = 7200.0
DEFAULT_MAX_LOOPS = 0
DEFAULT_MAX_TRANSIENT_RETRIES = 12
DEFAULT_RETRY_NOTIFY_AFTER_SECONDS = 300.0
DEFAULT_SINGLE_PROMPT_FILE = Path("PROMPT.md")
DEFAULT_SEQUENCE_PROMPT_FILE = Path("prompts.md")
DEFAULT_COMPLETION_MARKER = r"EXIT_SIGNAL:\s*true"

DEFAULT_STOP_PATTERNS = [
    r"rate[\s_-]*limit(?:ed|ing)?",
    r"\b429\b",
    r"too many requests",
    r"retry[\s_-]*after",
    r"back[\s_-]*off",
    r"quota exceeded",
    r"temporarily unavailable",
    r"overloaded",
    r"timed?\s*out",
    r"deadline exceeded",
    r"request aborted",
]
RETRYABLE_STOP_REASON_PATTERN = re.compile(
    r"rate[\s_-]*limit|"
    r"\b429\b|"
    r"too many requests|"
    r"retry[\s_-]*after|"
    r"back[\s_-]*off|"
    r"quota exceeded|"
    r"temporarily unavailable|"
    r"overloaded",
    re.IGNORECASE,
)
RATE_LIMIT_STOP_REASON_PATTERN = re.compile(
    r"rate[\s_-]*limit|"
    r"\b429\b|"
    r"too many requests|"
    r"quota exceeded",
    re.IGNORECASE,
)
RELATIVE_RETRY_DELAY_KEYS = (
    "retry_after_seconds",
    "retryAfterSeconds",
    "retry_after",
    "retryAfter",
    "reset_after_seconds",
    "resetAfterSeconds",
    "reset_after",
    "resetAfter",
)
ABSOLUTE_RETRY_RESET_KEYS = ("resetsAt", "reset_at", "resetAt")


class ConfigError(ValueError):
    """Raised when looper configuration is invalid."""


class PromptError(ValueError):
    """Raised when prompt loading fails."""


class CommandTemplateError(ValueError):
    """Raised when command templates cannot be rendered."""


@dataclass(frozen=True)
class AgentConfig:
    name: str
    kind: AgentKind
    cwd: Path = Path(".")
    extra_args: list[str] = field(default_factory=list)
    first_command: list[str] | None = None
    resume_command: list[str] | None = None
    env: dict[str, str] = field(default_factory=dict)
    scan_stdout_for_stop_patterns: bool = False


@dataclass(frozen=True)
class LooperConfig:
    default_agent: str = "codex"
    mode: LooperMode = "single"
    prompt_file: Path = DEFAULT_SINGLE_PROMPT_FILE
    mode_explicit: bool = False
    prompt_file_explicit: bool = False
    separator: str = r"^---\s*$"
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    sleep_seconds: float = 2.0
    fresh_session_per_loop: bool = True
    max_loops: int = DEFAULT_MAX_LOOPS
    max_transient_retries: int = DEFAULT_MAX_TRANSIENT_RETRIES
    retry_notify_after_seconds: float = DEFAULT_RETRY_NOTIFY_AFTER_SECONDS
    log_dir: Path = Path(".agent-looper/runs")
    stop_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_STOP_PATTERNS))
    kill_on_stop_pattern: bool = True
    ignore_nonzero: bool = False
    scan_stdout_for_stop_patterns: bool = False
    completion_enabled: bool = False
    completion_marker: str = DEFAULT_COMPLETION_MARKER
    completion_streak: int = 1
    plan_file: Path | None = None
    backup_enabled: bool = False
    backup_prefix: str = "looper-backup"
    backup_keep: int = 10
    cb_no_progress: int = 0


@dataclass(frozen=True)
class LoadedConfig:
    looper: LooperConfig
    agents: dict[str, AgentConfig]


@dataclass(frozen=True)
class RunOptions:
    agent_name: str | None
    config_path: Path
    mode: LooperMode | None = None
    prompt_file: Path | None = None
    agent_args: list[str] = field(default_factory=list)
    label: str | None = None
    timeout_seconds: float | None = None
    sleep_seconds: float | None = None
    max_loops: int | None = None
    max_transient_retries: int | None = None
    retry_notify_after_seconds: float | None = None
    completion_marker: str | None = None
    completion_streak: int | None = None
    plan_file: Path | None = None
    backup_enabled: bool = False
    backup_prefix: str | None = None
    backup_keep: int | None = None
    cb_no_progress: int | None = None
    preset: str | None = None
    once: bool = False
    fresh_session_per_loop: bool | None = None
    cwd: Path | None = None
    dry_run: bool = False
    ignore_nonzero: bool | None = None
    hold_on_stop: bool = False
    farm_session: str | None = None
    farm_attach: bool = False
    farm_add_bin: str = "codex-add"
    local: bool = False


@dataclass(frozen=True)
class CommandContext:
    prompt: str
    session: str
    session_id: str
    loop: int
    prompt_index: int
    label: str
    run_dir: Path

    def as_format_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "session": self.session,
            "session_id": self.session_id,
            "loop": self.loop,
            "prompt_index": self.prompt_index,
            "label": self.label,
            "run_dir": str(self.run_dir),
        }


@dataclass
class ParsedLine:
    session_id: str | None = None
    stop_reason: str | None = None
    retry_after_seconds: float | None = None
    retry_kind: str | None = None


@dataclass
class ProcessResult:
    returncode: int | None
    session_id: str | None = None
    stop_reason: str | None = None
    retry_after_seconds: float | None = None
    retry_kind: str | None = None
    timed_out: bool = False
    completion_detected: bool = False


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def default_agent_from_invocation() -> str:
    tool_name = os.environ.get("CODEX_TOOL_NAME", "").strip().lower()
    if tool_name in {"claude", "codex", "gemini"}:
        return tool_name

    script_name = Path(sys.argv[0]).name
    for name in ("claude", "gemini", "codex"):
        if script_name.startswith(f"{name}-"):
            return name
    return "codex"


def display_name() -> str:
    return os.environ.get("CODEX_LOOPER_DISPLAY_NAME") or Path(sys.argv[0]).name


def positive_float(value: str) -> float:
    out = float(value)
    if out <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return out


def nonnegative_float(value: str) -> float:
    out = float(value)
    if out < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return out


def nonnegative_int(value: str) -> int:
    out = int(value)
    if out < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return out


def positive_int(value: str) -> int:
    out = int(value)
    if out <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return out


def load_prompts(path: Path, separator: str) -> list[str]:
    if not path.exists():
        raise PromptError(f"prompt file not found: {path}")
    text = path.read_text(encoding="utf-8")
    parts = [part.strip() for part in re.split(separator, text, flags=re.MULTILINE)]
    prompts = [part for part in parts if part]
    if not prompts:
        raise PromptError(f"no prompts found in {path}")
    return prompts


def load_prompts_for_mode(path: Path, separator: str, mode: LooperMode) -> list[str]:
    if mode == "sequence":
        return load_prompts(path, separator)
    if mode != "single":
        raise ConfigError(f"unsupported looper mode: {mode}")
    if not path.exists():
        raise PromptError(f"prompt file not found: {path}")
    prompt = path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise PromptError(f"no prompts found in {path}")
    return [prompt]


def _path_exists_from(cwd: Path, path: Path) -> bool:
    return path.exists() if path.is_absolute() else (cwd / path).exists()


def resolve_prompt_defaults(looper: LooperConfig, *, cwd: Path) -> LooperConfig:
    prompt_file_is_default = looper.prompt_file == DEFAULT_SINGLE_PROMPT_FILE
    prompt_file_explicit = looper.prompt_file_explicit or not prompt_file_is_default
    mode = looper.mode
    prompt_file = looper.prompt_file

    if not looper.mode_explicit:
        if prompt_file_explicit:
            mode = "single" if prompt_file == DEFAULT_SINGLE_PROMPT_FILE else "sequence"
        elif (
            not _path_exists_from(cwd, DEFAULT_SINGLE_PROMPT_FILE)
            and _path_exists_from(cwd, DEFAULT_SEQUENCE_PROMPT_FILE)
        ):
            mode = "sequence"
            prompt_file = DEFAULT_SEQUENCE_PROMPT_FILE
        else:
            mode = "single"
            prompt_file = DEFAULT_SINGLE_PROMPT_FILE
    elif not prompt_file_explicit:
        prompt_file = DEFAULT_SEQUENCE_PROMPT_FILE if mode == "sequence" else DEFAULT_SINGLE_PROMPT_FILE

    return replace(looper, mode=mode, prompt_file=prompt_file)


def render_template(parts: list[str], context: CommandContext) -> list[str]:
    values = context.as_format_dict()
    rendered: list[str] = []
    formatter = Formatter()
    for part in parts:
        for _, field_name, _, _ in formatter.parse(part):
            if field_name and field_name not in values:
                raise CommandTemplateError(f"unknown command placeholder: {{{field_name}}}")
        try:
            rendered.append(part.format(**values))
        except KeyError as exc:
            raise CommandTemplateError(f"unknown command placeholder: {{{exc.args[0]}}}") from exc
    return rendered


def build_command(
    *,
    agent: AgentConfig,
    context: CommandContext,
    is_first_prompt_in_session: bool,
) -> list[str]:
    custom_template = agent.first_command if is_first_prompt_in_session else agent.resume_command
    if custom_template:
        return render_template(custom_template, context)

    if agent.kind == "claude":
        base = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
        base.extend(agent.extra_args)
        if is_first_prompt_in_session:
            return [*base, "--name", context.session, context.prompt]
        return [*base, "--resume", context.session, context.prompt]

    if agent.kind == "codex":
        base = ["codex", "exec", "--json"]
        base.extend(agent.extra_args)
        if is_first_prompt_in_session:
            return [*base, context.prompt]
        if context.session_id:
            return [*base, "resume", context.session_id, context.prompt]
        return [*base, "resume", "--last", context.prompt]

    if agent.kind == "generic":
        if not agent.first_command:
            raise CommandTemplateError(
                f"generic agent {agent.name!r} needs first_command in agent-looper.toml"
            )
        template = agent.first_command if is_first_prompt_in_session else agent.resume_command
        return render_template(template or agent.first_command, context)

    raise CommandTemplateError(f"unsupported agent kind: {agent.kind}")


def compile_stop_patterns(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(pattern, re.IGNORECASE) for pattern in patterns]


def compile_completion_marker(looper: LooperConfig) -> re.Pattern[str] | None:
    if not looper.completion_enabled:
        return None
    return re.compile(looper.completion_marker)


UNCHECKED_MARKDOWN_TASK_PATTERN = re.compile(r"^\s*[-*]\s+\[\s\]", re.MULTILINE)


def markdown_plan_has_unchecked_tasks(text: str) -> bool:
    return bool(UNCHECKED_MARKDOWN_TASK_PATTERN.search(text))


def plan_file_all_tasks_checked(path: Path) -> bool:
    if not path.exists():
        raise ConfigError(f"plan file not found: {path}")
    return not markdown_plan_has_unchecked_tasks(path.read_text(encoding="utf-8"))


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
    branch = f"{clean_prefix}/{stamp or utc_stamp()}-loop-{loop_number:04d}"
    try:
        _run_git(cwd, "branch", branch, "HEAD")
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or exc.stdout or str(exc)).strip()
        raise ConfigError(f"failed to create backup branch {branch}: {message}") from exc
    return branch


def prune_backup_branches(cwd: Path, *, prefix: str, keep: int) -> list[str]:
    if keep <= 0:
        return []
    ref_prefix = f"refs/heads/{prefix.rstrip('/')}"
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


def _status_path_from_porcelain_line(line: str) -> str:
    path = line[3:]
    if " -> " in path:
        path = path.rsplit(" -> ", 1)[1]
    return path.strip().strip('"')


def git_workspace_fingerprint(cwd: Path, ignored_paths: list[Path] | None = None) -> str | None:
    try:
        head = _run_git(cwd, "rev-parse", "HEAD").stdout.strip()
        status = _run_git(cwd, "status", "--porcelain=v1", "--untracked-files=all").stdout
    except subprocess.CalledProcessError:
        return None

    ignored_prefixes: list[str] = []
    for path in ignored_paths or []:
        try:
            relative = path if not path.is_absolute() else path.relative_to(cwd)
        except ValueError:
            continue
        normalized = str(relative).strip("/")
        if normalized:
            ignored_prefixes.append(normalized)

    lines = []
    for line in status.splitlines():
        status_path = _status_path_from_porcelain_line(line)
        if any(status_path == prefix or status_path.startswith(f"{prefix}/") for prefix in ignored_prefixes):
            continue
        lines.append(line)
    return head + "\n" + "\n".join(lines)


def _json_blob(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _match_text(text: str, patterns: list[re.Pattern[str]]) -> str | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def is_retryable_stop_reason(reason: str) -> bool:
    return bool(RETRYABLE_STOP_REASON_PATTERN.search(reason))


def classify_retry_kind(reason: str) -> str | None:
    if not is_retryable_stop_reason(reason):
        return None
    if RATE_LIMIT_STOP_REASON_PATTERN.search(reason):
        return "rate_limit"
    return "transient"


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


def _extract_retry_after_seconds(data: dict[str, Any], *, now_epoch: float) -> float | None:
    sources: list[dict[str, Any]] = [data]
    info = data.get("rate_limit_info")
    if isinstance(info, dict):
        sources.insert(0, info)

    for source in sources:
        for key in RELATIVE_RETRY_DELAY_KEYS:
            delay = _coerce_float(source.get(key))
            if delay is not None:
                return max(0.0, delay)

    for source in sources:
        for key in ABSOLUTE_RETRY_RESET_KEYS:
            reset_epoch = _coerce_float(source.get(key))
            if reset_epoch is not None:
                return max(0.0, reset_epoch - now_epoch)

    return None


def retry_delay_seconds(result: ProcessResult, looper: LooperConfig) -> float:
    if result.retry_after_seconds is not None:
        return max(0.0, result.retry_after_seconds)
    return looper.sleep_seconds


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:g}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:g}m"
    hours = minutes / 60
    return f"{hours:g}h"


def retry_status_message(*, result: ProcessResult, attempt: int, delay_seconds: float) -> str:
    kind = result.retry_kind or classify_retry_kind(result.stop_reason or "") or "retryable"
    reason = result.stop_reason or "retryable provider signal"
    return f"retrying {kind} attempt {attempt}; next in {format_duration(delay_seconds)}: {reason}"


def should_notify_retry_wait(*, delay_seconds: float, looper: LooperConfig) -> bool:
    return looper.retry_notify_after_seconds > 0 and delay_seconds >= looper.retry_notify_after_seconds


def retry_notification_message(retry_status: str) -> str:
    return f"Looper retry wait: {retry_status}"


def transient_retry_limit_message(*, result: ProcessResult, max_retries: int) -> str:
    reason = result.stop_reason or "retryable provider signal"
    return f"transient retry limit reached after {max_retries} attempts: {reason}"


def transient_retry_limit_reached(*, result: ProcessResult, retry_count: int, looper: LooperConfig) -> bool:
    if looper.max_transient_retries <= 0:
        return False
    retry_kind = result.retry_kind or classify_retry_kind(result.stop_reason or "")
    return retry_kind != "rate_limit" and retry_count > looper.max_transient_retries


def parse_output_line(
    *,
    line: str,
    stream: str,
    agent_kind: str,
    patterns: list[re.Pattern[str]],
    scan_stdout: bool,
    now_epoch: float | None = None,
) -> ParsedLine:
    stripped = line.strip()
    parsed = ParsedLine()
    now_epoch = time.time() if now_epoch is None else now_epoch

    data: Any | None = None
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            data = None

    if isinstance(data, dict):
        event_type = str(data.get("type", ""))
        subtype = str(data.get("subtype", ""))

        if agent_kind == "codex" and event_type == "thread.started":
            thread_id = data.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                parsed.session_id = thread_id

        if agent_kind == "claude":
            session_id = data.get("session_id") or data.get("conversation_id")
            if isinstance(session_id, str) and session_id:
                parsed.session_id = session_id

        if event_type == "rate_limit_event":
            info = data.get("rate_limit_info", {})
            status = info.get("status") if isinstance(info, dict) else None
            if status in {"allowed", "allowed_warning"}:
                return parsed
            if status == "rejected" or status is None:
                parsed.stop_reason = f"rate limit event: status={status or 'unknown'}"
                parsed.retry_after_seconds = _extract_retry_after_seconds(data, now_epoch=now_epoch)
                parsed.retry_kind = "rate_limit"
                return parsed

        if (
            event_type == "result"
            and subtype == "success"
            and data.get("is_error") is not True
            and data.get("api_error_status") in {None, ""}
        ):
            return parsed

        error_like = (
            event_type == "error"
            or event_type.endswith(".failed")
            or event_type in {"turn.failed", "result", "system", "auth_status"}
            or subtype not in {"", "success"}
            or "error" in data
            or "exception" in data
            or "rate_limit" in event_type.lower()
            or "backoff" in event_type.lower()
        )
        if error_like:
            match = _match_text(_json_blob(data), patterns)
            if match:
                parsed.stop_reason = f"stop pattern detected in structured {stream}: {match!r}"
                parsed.retry_after_seconds = _extract_retry_after_seconds(data, now_epoch=now_epoch)
                parsed.retry_kind = classify_retry_kind(match)
                return parsed

        return parsed

    if stream == "stderr" or scan_stdout:
        match = _match_text(line, patterns)
        if match:
            parsed.stop_reason = f"stop pattern detected in {stream}: {match!r}"
            parsed.retry_kind = classify_retry_kind(match)

    return parsed


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
) -> ProcessResult:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    merged_env = os.environ.copy()
    merged_env.update(env)

    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd),
        env=merged_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=(os.name == "posix"),
    )

    result = ProcessResult(returncode=None)
    stop_event = asyncio.Event()

    async def read_stream(reader: asyncio.StreamReader | None, stream_name: str) -> None:
        if reader is None:
            return
        output_stream = sys.stdout if stream_name == "stdout" else sys.stderr
        with log_path.open("a", encoding="utf-8") as log_file:
            while True:
                chunk = await reader.readline()
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                _safe_stream_write(output_stream, text)
                log_file.write(f"[{utc_stamp()}] {stream_name}: {text}")
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

    async def wait_for_stop() -> None:
        await stop_event.wait()
        if kill_on_stop_pattern:
            await _terminate_process_group(process)

    stdout_task = asyncio.create_task(read_stream(process.stdout, "stdout"))
    stderr_task = asyncio.create_task(read_stream(process.stderr, "stderr"))
    stop_task = asyncio.create_task(wait_for_stop())

    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        result.timed_out = True
        result.stop_reason = f"local timeout after {timeout_seconds:g} seconds"
        await _terminate_process_group(process)
    finally:
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        if not stop_task.done():
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
        await _close_subprocess_transport(process)

    result.returncode = process.returncode
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"[{utc_stamp()}] returncode: {result.returncode}\n")
        if result.stop_reason:
            log_file.write(f"[{utc_stamp()}] stop_reason: {result.stop_reason}\n")
    return result


def set_tmux_window_option(name: str, value: str) -> None:
    if not os.environ.get("TMUX"):
        return
    tmux = shutil.which("tmux")
    if not tmux:
        return
    subprocess.run([tmux, "set-window-option", "-q", name, value], check=False)


def display_tmux_message(message: str) -> None:
    if not os.environ.get("TMUX"):
        return
    tmux = shutil.which("tmux")
    if not tmux:
        return
    subprocess.run([tmux, "display-message", message], check=False)


def rename_current_window(label: str) -> None:
    if not os.environ.get("TMUX"):
        return
    tmux = shutil.which("tmux")
    if not tmux:
        return
    subprocess.run([tmux, "rename-window", label], check=False)


def make_label(label: str | None, agent_name: str) -> str:
    if label:
        return label
    return f"Looper_{secrets.token_hex(3)}"


def make_run_dir(log_dir: Path, label: str) -> Path:
    safe_label = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in label).strip("-")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return log_dir / f"{stamp}__{safe_label or 'agent'}"


def apply_run_options(config: LooperConfig, options: RunOptions) -> LooperConfig:
    updates: dict[str, Any] = {}
    if options.mode is not None:
        updates["mode"] = options.mode
        updates["mode_explicit"] = True
    if options.prompt_file is not None:
        updates["prompt_file"] = options.prompt_file
        updates["prompt_file_explicit"] = True
    if options.timeout_seconds is not None:
        updates["timeout_seconds"] = options.timeout_seconds
    if options.sleep_seconds is not None:
        updates["sleep_seconds"] = options.sleep_seconds
    if options.max_loops is not None:
        updates["max_loops"] = options.max_loops
    if options.max_transient_retries is not None:
        updates["max_transient_retries"] = options.max_transient_retries
    if options.retry_notify_after_seconds is not None:
        updates["retry_notify_after_seconds"] = options.retry_notify_after_seconds
    if options.completion_marker is not None:
        updates["completion_enabled"] = True
        updates["completion_marker"] = options.completion_marker
    if options.completion_streak is not None:
        updates["completion_streak"] = options.completion_streak
    if options.plan_file is not None:
        updates["plan_file"] = options.plan_file
    if options.backup_enabled:
        updates["backup_enabled"] = True
    if options.backup_prefix is not None:
        updates["backup_prefix"] = options.backup_prefix
    if options.backup_keep is not None:
        updates["backup_keep"] = options.backup_keep
    if options.cb_no_progress is not None:
        updates["cb_no_progress"] = options.cb_no_progress
    if options.once:
        updates["max_loops"] = 1
    if options.fresh_session_per_loop is not None:
        updates["fresh_session_per_loop"] = options.fresh_session_per_loop
    if options.ignore_nonzero is not None:
        updates["ignore_nonzero"] = options.ignore_nonzero
    return replace(config, **updates)


async def run_loop(*, agent: AgentConfig, looper: LooperConfig, options: RunOptions) -> int:
    looper = resolve_prompt_defaults(looper, cwd=Path.cwd())
    label = make_label(options.label, options.agent_name)
    run_dir = make_run_dir(looper.log_dir, label)
    run_dir.mkdir(parents=True, exist_ok=True)

    prompts = load_prompts_for_mode(looper.prompt_file, looper.separator, looper.mode)
    patterns = compile_stop_patterns(looper.stop_patterns)
    completion_pattern = compile_completion_marker(looper)

    if options.cwd is not None:
        agent = replace(agent, cwd=options.cwd)

    if not agent.cwd.exists():
        print(f"agent cwd does not exist: {agent.cwd}", file=sys.stderr)
        return 2

    if options.agent_args:
        agent = replace(agent, extra_args=[*agent.extra_args, *options.agent_args])

    rename_current_window(label)
    set_tmux_window_option(TMUX_STATE_OPTION, "RUN")
    set_tmux_window_option(TMUX_STOP_REASON_OPTION, "")

    print(f"agent: {agent.name} ({agent.kind})")
    print(f"label: {label}")
    print(f"mode: {looper.mode}")
    print(f"prompts: {len(prompts)} from {looper.prompt_file}")
    print(f"logs: {run_dir}")
    print(
        "session mode: "
        + ("fresh session per loop" if looper.fresh_session_per_loop else "one session reused across loops")
    )

    loop_number = 0
    completion_streak_count = 0
    no_progress_count = 0
    persistent_session_name = label
    persistent_session_id = ""
    fingerprint_ignored_paths = [run_dir.resolve()]

    while True:
        loop_number += 1
        loop_completion_detected = False
        progress_before = None
        session_name = (
            f"{label}-loop-{loop_number:04d}" if looper.fresh_session_per_loop else persistent_session_name
        )
        session_id = "" if looper.fresh_session_per_loop else persistent_session_id
        first_prompt_in_session = looper.fresh_session_per_loop or loop_number == 1

        print(f"\n===== loop {loop_number} / session {session_name} =====")

        if looper.cb_no_progress:
            progress_before = git_workspace_fingerprint(agent.cwd, ignored_paths=fingerprint_ignored_paths)
            if progress_before is None:
                raise ConfigError("cb_no_progress requires an agent cwd inside a git work tree")

        if looper.backup_enabled and not options.dry_run:
            backup_branch = create_backup_branch(
                agent.cwd,
                prefix=looper.backup_prefix,
                loop_number=loop_number,
            )
            print(f"backup branch: {backup_branch}")
            for pruned_branch in prune_backup_branches(
                agent.cwd,
                prefix=looper.backup_prefix,
                keep=looper.backup_keep,
            ):
                print(f"pruned backup branch: {pruned_branch}")

        for prompt_index, prompt in enumerate(prompts, start=1):
            attempt_first_prompt_in_session = first_prompt_in_session
            first_prompt_in_session = False
            retry_count = 0
            log_path = run_dir / f"loop-{loop_number:04d}__prompt-{prompt_index:03d}.log"

            while True:
                context = CommandContext(
                    prompt=prompt,
                    session=session_name,
                    session_id=session_id,
                    loop=loop_number,
                    prompt_index=prompt_index,
                    label=label,
                    run_dir=run_dir,
                )
                command = build_command(
                    agent=agent,
                    context=context,
                    is_first_prompt_in_session=attempt_first_prompt_in_session,
                )

                print(f"\n--- prompt {prompt_index}/{len(prompts)} ---")
                if retry_count:
                    print(f"retry attempt: {retry_count}")
                print(f"$ {shlex.join(command)}")

                if options.dry_run:
                    break

                result = await run_command(
                    command=command,
                    cwd=agent.cwd,
                    env=agent.env,
                    timeout_seconds=looper.timeout_seconds,
                    log_path=log_path,
                    agent_kind=agent.kind,
                    patterns=patterns,
                    scan_stdout=(
                        looper.scan_stdout_for_stop_patterns or agent.scan_stdout_for_stop_patterns
                    ),
                    kill_on_stop_pattern=looper.kill_on_stop_pattern,
                    completion_pattern=completion_pattern,
                )

                if result.completion_detected:
                    loop_completion_detected = True

                if result.session_id:
                    session_id = result.session_id
                    if not looper.fresh_session_per_loop:
                        persistent_session_id = session_id

                if result.stop_reason:
                    if is_retryable_stop_reason(result.stop_reason):
                        retry_count += 1
                        if transient_retry_limit_reached(
                            result=result,
                            retry_count=retry_count,
                            looper=looper,
                        ):
                            limit_reason = transient_retry_limit_message(
                                result=result,
                                max_retries=looper.max_transient_retries,
                            )
                            print(f"\nSTOP: {limit_reason}")
                            print(f"last log: {log_path}")
                            set_tmux_window_option(TMUX_STATE_OPTION, "READY")
                            set_tmux_window_option(TMUX_STOP_REASON_OPTION, limit_reason[:250])
                            return 0
                        if result.session_id or agent.kind == "claude":
                            attempt_first_prompt_in_session = False
                        delay_seconds = retry_delay_seconds(result, looper)
                        retry_status = retry_status_message(
                            result=result,
                            attempt=retry_count,
                            delay_seconds=delay_seconds,
                        )
                        set_tmux_window_option(TMUX_STATE_OPTION, "RUN")
                        set_tmux_window_option(TMUX_STOP_REASON_OPTION, retry_status[:250])
                        if should_notify_retry_wait(delay_seconds=delay_seconds, looper=looper):
                            display_tmux_message(retry_notification_message(retry_status))
                        print(f"\nRETRY: {result.stop_reason}")
                        print(f"last log: {log_path}")
                        print(f"sleeping {format_duration(delay_seconds)} before retry")
                        await asyncio.sleep(delay_seconds)
                        continue

                    print(f"\nSTOP: {result.stop_reason}")
                    print(f"last log: {log_path}")
                    set_tmux_window_option(TMUX_STATE_OPTION, "READY")
                    set_tmux_window_option(TMUX_STOP_REASON_OPTION, result.stop_reason[:250])
                    return 0

                if result.returncode not in (0, None) and not looper.ignore_nonzero:
                    print(f"\nSTOP: command exited with code {result.returncode}")
                    print(f"last log: {log_path}")
                    set_tmux_window_option(TMUX_STATE_OPTION, "ERR")
                    set_tmux_window_option(TMUX_STOP_REASON_OPTION, f"exit {result.returncode}")
                    return int(result.returncode or 1)

                set_tmux_window_option(TMUX_STOP_REASON_OPTION, "")
                break

        print(f"\ncompleted loop {loop_number}")

        if looper.completion_enabled:
            if loop_completion_detected:
                if looper.plan_file and not plan_file_all_tasks_checked(looper.plan_file):
                    completion_streak_count = 0
                    print(
                        "completion marker detected, but plan file still has unchecked tasks: "
                        f"{looper.plan_file}"
                    )
                else:
                    completion_streak_count += 1
                    print(
                        "completion marker detected "
                        f"({completion_streak_count}/{looper.completion_streak})"
                    )
                    if completion_streak_count >= looper.completion_streak:
                        print("completion streak reached; stopping")
                        set_tmux_window_option(TMUX_STATE_OPTION, "READY")
                        set_tmux_window_option(TMUX_STOP_REASON_OPTION, "completion marker")
                        return 0
            else:
                if completion_streak_count:
                    print("completion marker missing; resetting completion streak")
                completion_streak_count = 0

        if looper.cb_no_progress:
            progress_after = git_workspace_fingerprint(agent.cwd, ignored_paths=fingerprint_ignored_paths)
            if progress_after is None:
                raise ConfigError("cb_no_progress requires an agent cwd inside a git work tree")
            if progress_after == progress_before:
                no_progress_count += 1
                print(f"no git progress detected ({no_progress_count}/{looper.cb_no_progress})")
                if no_progress_count >= looper.cb_no_progress:
                    reason = f"no git progress for {no_progress_count} loop(s)"
                    print(f"STOP: {reason}")
                    set_tmux_window_option(TMUX_STATE_OPTION, "READY")
                    set_tmux_window_option(TMUX_STOP_REASON_OPTION, reason[:250])
                    return 0
            else:
                no_progress_count = 0

        if options.dry_run:
            print("dry run complete")
            set_tmux_window_option(TMUX_STATE_OPTION, "READY")
            return 0

        if looper.max_loops and loop_number >= looper.max_loops:
            print(f"max loops reached: {looper.max_loops}")
            set_tmux_window_option(TMUX_STATE_OPTION, "READY")
            return 0

        await asyncio.sleep(looper.sleep_seconds)


def run_loop_sync(*, agent: AgentConfig, looper: LooperConfig, options: RunOptions) -> int:
    return asyncio.run(run_loop(agent=agent, looper=looper, options=options))


def _strip_toml_comment(line: str) -> str:
    quote = ""
    escaped = False
    for index, char in enumerate(line):
        if quote:
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "#":
            return line[:index].strip()
    return line.strip()


def _split_toml_assignment(text: str, *, line_no: int) -> tuple[str, str]:
    quote = ""
    escaped = False
    depth = 0
    for index, char in enumerate(text):
        if quote:
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == "=" and depth == 0:
            return text[:index].strip(), text[index + 1 :].strip()
    raise ConfigError(f"expected key = value on TOML line {line_no}")


def _split_toml_items(text: str, *, line_no: int) -> list[str]:
    items: list[str] = []
    quote = ""
    escaped = False
    depth = 0
    start = 0
    for index, char in enumerate(text):
        if quote:
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth < 0:
                raise ConfigError(f"unbalanced TOML value on line {line_no}")
        elif char == "," and depth == 0:
            items.append(text[start:index].strip())
            start = index + 1
    if quote or depth != 0:
        raise ConfigError(f"unbalanced TOML value on line {line_no}")
    tail = text[start:].strip()
    if tail:
        items.append(tail)
    return items


def _parse_basic_toml_key(text: str, *, line_no: int) -> str:
    key = text.strip()
    if not key:
        raise ConfigError(f"empty TOML key on line {line_no}")
    if key.startswith('"') or key.startswith("'"):
        value = _parse_basic_toml_value(key, line_no=line_no)
        if not isinstance(value, str):
            raise ConfigError(f"TOML key must be a string on line {line_no}")
        return value
    if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
        raise ConfigError(f"unsupported TOML key {key!r} on line {line_no}")
    return key


def _parse_basic_toml_value(text: str, *, line_no: int) -> Any:
    value = text.strip()
    if not value:
        raise ConfigError(f"empty TOML value on line {line_no}")
    if value.startswith('"'):
        decoder = json.JSONDecoder()
        try:
            decoded, end = decoder.raw_decode(value)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid TOML string on line {line_no}: {exc.msg}") from exc
        if value[end:].strip():
            raise ConfigError(f"unexpected TOML text after string on line {line_no}")
        if not isinstance(decoded, str):
            raise ConfigError(f"TOML string expected on line {line_no}")
        return decoded
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise ConfigError(f"invalid TOML literal string on line {line_no}")
        return value[1:-1]
    if value.startswith("["):
        if not value.endswith("]"):
            raise ConfigError(f"invalid TOML array on line {line_no}")
        body = value[1:-1].strip()
        if not body:
            return []
        return [
            _parse_basic_toml_value(item, line_no=line_no)
            for item in _split_toml_items(body, line_no=line_no)
        ]
    if value.startswith("{"):
        if not value.endswith("}"):
            raise ConfigError(f"invalid TOML inline table on line {line_no}")
        body = value[1:-1].strip()
        table: dict[str, Any] = {}
        if not body:
            return table
        for item in _split_toml_items(body, line_no=line_no):
            key_text, value_text = _split_toml_assignment(item, line_no=line_no)
            key = _parse_basic_toml_key(key_text, line_no=line_no)
            if key in table:
                raise ConfigError(f"duplicate TOML key {key!r} on line {line_no}")
            table[key] = _parse_basic_toml_value(value_text, line_no=line_no)
        return table
    if value == "true":
        return True
    if value == "false":
        return False
    numeric = value.replace("_", "")
    if re.fullmatch(r"[+-]?\d+", numeric):
        return int(numeric)
    if re.fullmatch(r"[+-]?(?:\d+\.\d*|\d*\.\d+|\d+)(?:[eE][+-]?\d+)?", numeric):
        return float(numeric)
    raise ConfigError(f"unsupported TOML value {value!r} on line {line_no}")


def parse_basic_toml(text: str) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    current = raw
    for line_no, original_line in enumerate(text.splitlines(), start=1):
        line = _strip_toml_comment(original_line)
        if not line:
            continue
        if line.startswith("["):
            if not line.endswith("]") or line.startswith("[["):
                raise ConfigError(f"unsupported TOML table header on line {line_no}")
            section = line[1:-1].strip()
            if not section:
                raise ConfigError(f"empty TOML table header on line {line_no}")
            current = raw
            for part in section.split("."):
                key = _parse_basic_toml_key(part, line_no=line_no)
                value = current.setdefault(key, {})
                if not isinstance(value, dict):
                    raise ConfigError(
                        f"TOML table {section!r} conflicts with existing key on line {line_no}"
                    )
                current = value
            continue
        key_text, value_text = _split_toml_assignment(line, line_no=line_no)
        key = _parse_basic_toml_key(key_text, line_no=line_no)
        if key in current:
            raise ConfigError(f"duplicate TOML key {key!r} on line {line_no}")
        current[key] = _parse_basic_toml_value(value_text, line_no=line_no)
    return raw


def _as_str_list(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{key} must be a list of strings")
    return list(value)


def _as_str_dict(value: Any, key: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a table/object")
    out: dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ConfigError(f"{key} must contain only string keys and string values")
        out[k] = v
    return out


def _path(value: Any, default: Path) -> Path:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError("path values must be strings")
    return Path(value)


def _optional_path(value: Any, key: str) -> Path | None:
    if value in {None, ""}:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string path")
    return Path(value)


def default_agents() -> dict[str, AgentConfig]:
    return {
        "claude": AgentConfig(name="claude", kind="claude"),
        "codex": AgentConfig(name="codex", kind="codex"),
        "gemini": AgentConfig(
            name="gemini",
            kind="generic",
            first_command=["gemini", "-p", "{prompt}"],
            resume_command=["gemini", "-p", "{prompt}"],
            scan_stdout_for_stop_patterns=True,
        ),
    }


def read_config_raw(path: Path) -> dict[str, Any]:
    if path.exists():
        if tomllib is None:
            return parse_basic_toml(path.read_text(encoding="utf-8"))
        else:
            with path.open("rb") as fh:
                return tomllib.load(fh)
    return {}


def merge_raw_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = merge_raw_config(current, value)
        else:
            merged[key] = value
    return merged


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def resolve_preset_path(spec: str) -> Path:
    direct = Path(spec).expanduser()
    if direct.exists():
        return direct
    if direct.is_absolute() or direct.parent != Path(".") or direct.suffix:
        raise ConfigError(f"preset not found: {spec}")

    name = f"{spec}.toml"
    candidates = [
        Path.home() / ".config" / "codexfarm" / "presets" / name,
        repo_root() / "examples" / "presets" / name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise ConfigError(f"preset {spec!r} not found; searched: {searched}")


def load_config(path: Path, *, preset_paths: list[Path] | None = None) -> LoadedConfig:
    raw = read_config_raw(path)
    for preset_path in preset_paths or []:
        raw = merge_raw_config(raw, read_config_raw(preset_path))

    raw_looper = raw.get("looper", {})
    if raw_looper is None:
        raw_looper = {}
    if not isinstance(raw_looper, dict):
        raise ConfigError("[looper] must be a TOML table")

    default_looper = LooperConfig()
    raw_mode = str(raw_looper.get("mode", default_looper.mode))
    if raw_mode not in {"single", "sequence"}:
        raise ConfigError("looper.mode must be single or sequence")
    completion_streak = int(raw_looper.get("completion_streak", default_looper.completion_streak))
    if completion_streak <= 0:
        raise ConfigError("looper.completion_streak must be greater than zero")
    backup_keep = int(raw_looper.get("backup_keep", default_looper.backup_keep))
    if backup_keep < 0:
        raise ConfigError("looper.backup_keep must be zero or greater")
    cb_no_progress = int(raw_looper.get("cb_no_progress", default_looper.cb_no_progress))
    if cb_no_progress < 0:
        raise ConfigError("looper.cb_no_progress must be zero or greater")
    looper = LooperConfig(
        default_agent=str(raw_looper.get("default_agent", default_looper.default_agent)),
        mode=raw_mode,  # type: ignore[arg-type]
        mode_explicit="mode" in raw_looper,
        prompt_file=_path(raw_looper.get("prompt_file"), default_looper.prompt_file),
        prompt_file_explicit="prompt_file" in raw_looper,
        separator=str(raw_looper.get("separator", default_looper.separator)),
        timeout_seconds=float(raw_looper.get("timeout_seconds", default_looper.timeout_seconds)),
        sleep_seconds=float(raw_looper.get("sleep_seconds", default_looper.sleep_seconds)),
        fresh_session_per_loop=bool(
            raw_looper.get("fresh_session_per_loop", default_looper.fresh_session_per_loop)
        ),
        max_loops=int(raw_looper.get("max_loops", default_looper.max_loops)),
        max_transient_retries=int(
            raw_looper.get("max_transient_retries", default_looper.max_transient_retries)
        ),
        retry_notify_after_seconds=float(
            raw_looper.get("retry_notify_after_seconds", default_looper.retry_notify_after_seconds)
        ),
        log_dir=_path(raw_looper.get("log_dir"), default_looper.log_dir),
        stop_patterns=_as_str_list(raw_looper.get("stop_patterns"), "looper.stop_patterns")
        or list(default_looper.stop_patterns),
        kill_on_stop_pattern=bool(
            raw_looper.get("kill_on_stop_pattern", default_looper.kill_on_stop_pattern)
        ),
        ignore_nonzero=bool(raw_looper.get("ignore_nonzero", default_looper.ignore_nonzero)),
        scan_stdout_for_stop_patterns=bool(
            raw_looper.get(
                "scan_stdout_for_stop_patterns",
                default_looper.scan_stdout_for_stop_patterns,
            )
        ),
        completion_enabled=bool(
            raw_looper.get("completion_enabled", default_looper.completion_enabled)
        ),
        completion_marker=str(
            raw_looper.get("completion_marker", default_looper.completion_marker)
        ),
        completion_streak=completion_streak,
        plan_file=_optional_path(raw_looper.get("plan_file"), "looper.plan_file"),
        backup_enabled=bool(raw_looper.get("backup_enabled", default_looper.backup_enabled)),
        backup_prefix=str(raw_looper.get("backup_prefix", default_looper.backup_prefix)),
        backup_keep=backup_keep,
        cb_no_progress=cb_no_progress,
    )

    agents = default_agents()
    raw_agents = raw.get("agents", {})
    if raw_agents is None:
        raw_agents = {}
    if not isinstance(raw_agents, dict):
        raise ConfigError("[agents] must be a TOML table")

    for name, value in raw_agents.items():
        if not isinstance(value, dict):
            raise ConfigError(f"[agents.{name}] must be a TOML table")
        base = agents.get(name)
        kind = str(value.get("kind", base.kind if base else name))
        if kind not in {"claude", "codex", "generic"}:
            raise ConfigError(f"agents.{name}.kind must be claude, codex, or generic")
        agents[name] = AgentConfig(
            name=name,
            kind=kind,  # type: ignore[arg-type]
            cwd=_path(value.get("cwd"), base.cwd if base else Path(".")),
            extra_args=_as_str_list(value.get("extra_args"), f"agents.{name}.extra_args")
            or (base.extra_args if base else []),
            first_command=(
                _as_str_list(value.get("first_command"), f"agents.{name}.first_command")
                or (base.first_command if base else None)
            ),
            resume_command=(
                _as_str_list(value.get("resume_command"), f"agents.{name}.resume_command")
                or (base.resume_command if base else None)
            ),
            env={**(base.env if base else {}), **_as_str_dict(value.get("env"), f"agents.{name}.env")},
            scan_stdout_for_stop_patterns=bool(
                value.get(
                    "scan_stdout_for_stop_patterns",
                    base.scan_stdout_for_stop_patterns if base else False,
                )
            ),
        )

    return LoadedConfig(looper=looper, agents=agents)


def build_example_config(
    *,
    default_agent: str = "codex",
    mode: LooperMode = "single",
    prompt_file: str = "PROMPT.md",
    timeout_seconds: str = "7200",
    sleep_seconds: str = "2",
    max_loops: str = "0",
    max_transient_retries: str = "12",
    retry_notify_after_seconds: str = "300",
) -> str:
    return f"""# agent-looper.toml
# Default mode loops one prompt file forever. Set mode = "sequence" to split
# prompt_file on lines containing only ---.

[looper]
default_agent = {json.dumps(default_agent)}
mode = {json.dumps(mode)}
prompt_file = {json.dumps(prompt_file)}
timeout_seconds = {timeout_seconds}
sleep_seconds = {sleep_seconds}
fresh_session_per_loop = true
# max_loops = 0 means forever. Use --once or --max-loops for a bounded run.
max_loops = {max_loops}
# Rate-limit retries are uncapped; this caps repeated non-rate-limit transient retries.
# max_transient_retries = 0 means unlimited.
max_transient_retries = {max_transient_retries}
# Notify the tmux pane for retry waits at or above this many seconds. 0 disables.
retry_notify_after_seconds = {retry_notify_after_seconds}
log_dir = ".agent-looper/runs"

[agents.claude]
kind = "claude"
# extra_args are inserted into the built-in Claude Code command templates.
# Example:
# extra_args = ["--permission-mode", "acceptEdits", "--max-turns", "20"]
# For isolated unattended sandboxes, Claude also supports:
# extra_args = ["--dangerously-skip-permissions"]
extra_args = []

[agents.codex]
kind = "codex"
# Example:
# extra_args = ["--sandbox", "workspace-write"]
extra_args = []

[agents.gemini]
kind = "generic"
# Gemini CLI prompt/resume flags may vary by version; override these templates
# if your installed Gemini CLI uses a different non-interactive interface.
first_command = ["gemini", "-p", "{{prompt}}"]
resume_command = ["gemini", "-p", "{{prompt}}"]
scan_stdout_for_stop_patterns = true

# For other coding agents, define command templates. Placeholders are:
# {{prompt}}, {{session}}, {{session_id}}, {{loop}}, {{prompt_index}}, {{label}}, {{run_dir}}
#
# [agents.my_agent]
# kind = "generic"
# first_command = ["my-agent", "run", "--session", "{{session}}", "{{prompt}}"]
# resume_command = ["my-agent", "run", "--resume", "{{session}}", "{{prompt}}"]
# scan_stdout_for_stop_patterns = true
"""


EXAMPLE_CONFIG = build_example_config()


EXAMPLE_PROMPTS = """Summarize the repository layout. Identify one safe cleanup, but do not modify files unless the cleanup is obvious and low-risk.
"""


def write_starter_files(
    *,
    force: bool,
    config_text: str = EXAMPLE_CONFIG,
    prompts_text: str = EXAMPLE_PROMPTS,
    prompt_path: Path = DEFAULT_SINGLE_PROMPT_FILE,
) -> list[Path]:
    files = [
        (Path("agent-looper.toml"), config_text),
        (prompt_path, prompts_text),
    ]
    written: list[Path] = []
    for path, content in files:
        if path.exists() and not force:
            print(f"exists, leaving unchanged: {path}")
            continue
        path.write_text(content, encoding="utf-8")
        written.append(path)
        print(f"wrote {path}")
    return written


def guidance_lines(*, initialized: bool, prompt_path: Path = DEFAULT_SINGLE_PROMPT_FILE) -> list[str]:
    heading = (
        "Initialized Agent Looper starter files."
        if initialized
        else "Agent Looper is already initialized."
    )
    return [
        heading,
        "",
        "Next steps:",
        f"  1. Edit {prompt_path}",
        "  2. Run in the default tmux farm: codex-looper",
        "  3. Inspect activity: codex-status activity",
        "",
        "Customize defaults with: codex-looper init --interactive --force",
    ]


def print_guidance(*, initialized: bool, prompt_path: Path = DEFAULT_SINGLE_PROMPT_FILE) -> None:
    print("\n".join(guidance_lines(initialized=initialized, prompt_path=prompt_path)))


def first_run_main() -> int:
    config = Path("agent-looper.toml")
    already_initialized = config.exists() and (
        DEFAULT_SINGLE_PROMPT_FILE.exists() or DEFAULT_SEQUENCE_PROMPT_FILE.exists()
    )
    written = write_starter_files(force=False)
    print("")
    print_guidance(initialized=bool(written) and not already_initialized)
    return 0


def _ask(prompt: str, default: str) -> str:
    reply = input(f"{prompt} [{default}]: ").strip()
    return reply or default


def _ask_number(prompt: str, default: str) -> str:
    value = _ask(prompt, default)
    try:
        parsed = float(value)
    except ValueError:
        print(f"Invalid number {value!r}; using {default}.")
        return default
    if parsed <= 0:
        print(f"Number must be greater than zero; using {default}.")
        return default
    return value


def _ask_nonnegative_number(prompt: str, default: str) -> str:
    value = _ask(prompt, default)
    try:
        parsed = float(value)
    except ValueError:
        print(f"Invalid number {value!r}; using {default}.")
        return default
    if parsed < 0:
        print(f"Number must be zero or greater; using {default}.")
        return default
    return value


def _ask_nonnegative_int(prompt: str, default: str) -> str:
    value = _ask(prompt, default)
    try:
        parsed = int(value)
    except ValueError:
        print(f"Invalid integer {value!r}; using {default}.")
        return default
    if parsed < 0:
        print(f"Number must be zero or greater; using {default}.")
        return default
    return str(parsed)


def interactive_starter_content() -> tuple[str, str, Path]:
    print("Agent Looper interactive setup")
    default_agent = _ask("Default agent (codex, claude, gemini)", "codex")
    if default_agent not in default_agents():
        print(f"Unknown default agent {default_agent!r}; using codex.")
        default_agent = "codex"
    timeout_seconds = _ask_number("Timeout seconds per prompt", "7200")
    sleep_seconds = _ask_number("Sleep seconds between loops", "2")
    max_loops = _ask_nonnegative_int("Max loops (0 means forever)", "0")
    max_transient_retries = _ask_nonnegative_int(
        "Max transient retries before stopping (0 means unlimited)",
        "12",
    )
    retry_notify_after_seconds = _ask_nonnegative_number(
        "Notify for retry waits at or above seconds (0 disables)",
        "300",
    )

    print("Enter prompts one at a time. Submit a blank prompt when finished.")
    prompts: list[str] = []
    while True:
        prompt = input(f"Prompt {len(prompts) + 1}: ").strip()
        if not prompt:
            break
        prompts.append(prompt)
    if len(prompts) > 1:
        mode: LooperMode = "sequence"
        prompt_path = DEFAULT_SEQUENCE_PROMPT_FILE
        prompt_text = "\n---\n".join(prompts).strip() + "\n"
    else:
        mode = "single"
        prompt_path = DEFAULT_SINGLE_PROMPT_FILE
        prompt_text = (prompts[0].strip() + "\n") if prompts else EXAMPLE_PROMPTS
    return (
        build_example_config(
            default_agent=default_agent,
            mode=mode,
            prompt_file=str(prompt_path),
            timeout_seconds=timeout_seconds,
            sleep_seconds=sleep_seconds,
            max_loops=max_loops,
            max_transient_retries=max_transient_retries,
            retry_notify_after_seconds=retry_notify_after_seconds,
        ),
        prompt_text,
        prompt_path,
    )


def clean_farm_args(argv: list[str]) -> list[str]:
    cleaned: list[str] = []
    in_agent_args = False
    index = 0
    while index < len(argv):
        item = argv[index]
        if in_agent_args:
            cleaned.append(item)
            index += 1
            continue
        if item == "--":
            cleaned.append(item)
            in_agent_args = True
            index += 1
            continue
        if item == "--farm-session":
            next_index = index + 1
            if next_index < len(argv) and not argv[next_index].startswith("-"):
                index = next_index + 1
            else:
                index += 1
            continue
        if item == "--farm-add-bin":
            index += 2
            continue
        if item.startswith("--farm-session=") or item.startswith("--farm-add-bin="):
            index += 1
            continue
        if item == "--farm-attach":
            index += 1
            continue
        cleaned.append(item)
        index += 1
    return cleaned


def _argv_has_option(argv: list[str], *names: str) -> bool:
    for item in argv:
        for name in names:
            if item == name or item.startswith(f"{name}="):
                return True
    return False


def maybe_launch_farm(options: RunOptions, original_argv: list[str]) -> int | None:
    if options.local or options.farm_session is None:
        return None

    executable = os.environ.get("CODEX_LOOPER_COMMAND") or shutil.which(Path(sys.argv[0]).name) or sys.argv[0]
    executable = str(Path(executable).resolve())
    cwd = options.cwd or Path.cwd()
    env = os.environ.copy()
    env["CODEX_NAME"] = options.label or cwd.name
    env["CODEX_CMD"] = executable
    inner_args = clean_farm_args(original_argv)
    if not _argv_has_option(inner_args, "--local"):
        inner_args.append("--local")
    if options.label and not _argv_has_option(inner_args, "--label", "-l"):
        inner_args.extend(["--label", options.label])
    env["CODEX_ARGS"] = shlex.join(inner_args)

    command = [options.farm_add_bin]
    if not options.farm_attach:
        command.append("-d")
    if options.farm_session:
        command.append(options.farm_session)
    command.append(str(cwd))
    return subprocess.run(command, env=env, check=False).returncode


def split_agent_args(argv: list[str]) -> tuple[list[str], list[str]]:
    if "--" not in argv:
        return list(argv), []
    index = argv.index("--")
    return argv[:index], argv[index + 1 :]


def add_run_arguments(parser: argparse.ArgumentParser, *, default_agent: str | None = None) -> None:
    parser.add_argument("-a", "--agent", default=None, help="agent config name")
    parser.add_argument("-c", "--config", default="agent-looper.toml", help="config file path")
    parser.add_argument("--preset", help="preset name or TOML path to layer over project config")
    parser.add_argument("--mode", choices=("single", "sequence"), help="prompt loading mode")
    parser.add_argument("-p", "--prompt-file", help="prompt file")
    parser.add_argument("-l", "--label", help="human-readable run label; also used for resumable sessions")
    parser.add_argument("--timeout", type=positive_float, help="timeout seconds per prompt")
    parser.add_argument("--sleep", type=positive_float, help="sleep seconds between loops")
    parser.add_argument("--max-loops", type=nonnegative_int, help="0 means forever")
    parser.add_argument(
        "--max-transient-retries",
        type=nonnegative_int,
        help="cap non-rate-limit transient retries; 0 means unlimited",
    )
    parser.add_argument(
        "--retry-notify-after",
        dest="retry_notify_after_seconds",
        type=nonnegative_float,
        help="show tmux notification for retry waits at or above this many seconds; 0 disables",
    )
    parser.add_argument(
        "--complete-on",
        dest="completion_marker",
        help="stop after a completed loop whose output matches this regex",
    )
    parser.add_argument(
        "--completion-streak",
        type=positive_int,
        help="completion marker matches required on consecutive loops before stopping",
    )
    parser.add_argument(
        "--plan-file",
        help="markdown checklist gate; completion requires no unchecked - [ ] tasks",
    )
    parser.add_argument("--backup", dest="backup_enabled", action="store_true", help="create git backup branches")
    parser.add_argument("--backup-prefix", help="git branch prefix for backup branches")
    parser.add_argument("--backup-keep", type=nonnegative_int, help="number of newest backup branches to keep")
    parser.add_argument(
        "--cb-no-progress",
        type=nonnegative_int,
        help="stop after this many completed loops with no git workspace change; 0 disables",
    )
    parser.add_argument("--once", action="store_true", help="run the sequence once, then stop")
    session_group = parser.add_mutually_exclusive_group()
    session_group.add_argument(
        "--fresh-session-per-loop",
        dest="fresh_session_per_loop",
        action="store_true",
        default=None,
        help="start a new agent session for each completed loop",
    )
    session_group.add_argument(
        "--reuse-session",
        dest="fresh_session_per_loop",
        action="store_false",
        help="reuse one agent session across all loops",
    )
    parser.add_argument("--cwd", help="working directory for the agent command")
    parser.add_argument("--dry-run", action="store_true", help="print commands without running them")
    parser.add_argument("--ignore-nonzero", action="store_true", default=None, help="do not stop on nonzero exit")
    parser.add_argument("--stop-on-nonzero", dest="ignore_nonzero", action="store_false", help="stop on nonzero exit")
    parser.add_argument("--hold-on-stop", action="store_true", help="wait for Enter before exiting after a stop")
    parser.add_argument("--local", action="store_true", help="run in this terminal instead of the default farm")
    parser.add_argument(
        "--farm-session",
        nargs="?",
        const="",
        help="launch this looper in a Codex CLI Farm tmux session; omit NAME to use the default",
    )
    parser.add_argument("--farm-attach", action="store_true", help="attach after launching with --farm-session")
    parser.add_argument("--farm-add-bin", default="codex-add", help="codex-add-compatible launcher")
    parser.add_argument("--version", action="version", version=f"codex-looper {VERSION}")
    parser.epilog = (
        "Pass built-in agent arguments after --, for example: "
        "claude-looper -- --dangerously-skip-permissions"
    )


def resolve_agent_name(
    *,
    explicit_agent: str | None,
    looper: LooperConfig,
    invocation_default: str | None,
) -> str:
    if explicit_agent:
        return explicit_agent
    if invocation_default and invocation_default != "codex":
        return invocation_default
    return looper.default_agent or invocation_default or default_agent_from_invocation()


def parse_run_options(args: argparse.Namespace, *, agent_args: list[str] | None = None) -> RunOptions:
    return RunOptions(
        agent_name=args.agent,
        config_path=Path(args.config),
        mode=args.mode,
        prompt_file=Path(args.prompt_file) if args.prompt_file else None,
        agent_args=list(agent_args or []),
        label=args.label,
        timeout_seconds=args.timeout,
        sleep_seconds=args.sleep,
        max_loops=args.max_loops,
        max_transient_retries=args.max_transient_retries,
        retry_notify_after_seconds=args.retry_notify_after_seconds,
        completion_marker=args.completion_marker,
        completion_streak=args.completion_streak,
        plan_file=Path(args.plan_file) if args.plan_file else None,
        backup_enabled=args.backup_enabled,
        backup_prefix=args.backup_prefix,
        backup_keep=args.backup_keep,
        cb_no_progress=args.cb_no_progress,
        preset=args.preset,
        once=args.once,
        fresh_session_per_loop=args.fresh_session_per_loop,
        cwd=Path(args.cwd) if args.cwd else None,
        dry_run=args.dry_run,
        ignore_nonzero=args.ignore_nonzero,
        hold_on_stop=args.hold_on_stop,
        farm_session=args.farm_session,
        farm_attach=args.farm_attach,
        farm_add_bin=args.farm_add_bin,
        local=args.local,
    )


def maybe_hold_on_stop(options: RunOptions) -> None:
    if not options.hold_on_stop or not sys.stdin.isatty():
        return
    try:
        input("Looper stopped. Press Enter to close this window.")
    except EOFError:
        return


def run_command_main(argv: list[str] | None = None, *, default_agent: str | None = None) -> int:
    real_argv = list(sys.argv[1:] if argv is None else argv)
    looper_argv, agent_args = split_agent_args(real_argv)
    parser = argparse.ArgumentParser(
        prog=display_name(),
        description="Loop a coding-agent prompt sequence until timeout, rate-limit, or backoff signals appear.",
    )
    add_run_arguments(parser, default_agent=default_agent)
    args = parser.parse_args(looper_argv)
    options = parse_run_options(args, agent_args=agent_args)
    if options.label is None:
        options = replace(options, label=make_label(None, default_agent or "agent"))
    if options.farm_session is None and not options.local and not options.dry_run:
        options = replace(options, farm_session="")

    farm_result = maybe_launch_farm(options, real_argv)
    if farm_result is not None:
        return farm_result

    try:
        preset_paths = [resolve_preset_path(options.preset)] if options.preset else []
        loaded = load_config(options.config_path, preset_paths=preset_paths)
        agent_name = resolve_agent_name(
            explicit_agent=options.agent_name,
            looper=loaded.looper,
            invocation_default=default_agent,
        )
        options = replace(options, agent_name=agent_name)
        if agent_name not in loaded.agents:
            available = ", ".join(sorted(loaded.agents))
            raise ConfigError(f"unknown agent {agent_name!r}; available: {available}")
        looper = apply_run_options(loaded.looper, options)
        agent = loaded.agents[agent_name]
        result = run_loop_sync(agent=agent, looper=looper, options=options)
    except (ConfigError, PromptError, CommandTemplateError) as exc:
        print(f"looper error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    maybe_hold_on_stop(options)
    return result


def init_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"{display_name()} init", description="Create starter config and prompt files.")
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    parser.add_argument("-i", "--interactive", action="store_true", help="ask for defaults and prompt text")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    config_text = EXAMPLE_CONFIG
    prompts_text = EXAMPLE_PROMPTS
    prompt_path = DEFAULT_SINGLE_PROMPT_FILE
    if args.interactive:
        config_text, prompts_text, prompt_path = interactive_starter_content()

    write_starter_files(
        force=args.force,
        config_text=config_text,
        prompts_text=prompts_text,
        prompt_path=prompt_path,
    )
    print("")
    print("Starter files are ready.")
    print_guidance(initialized=True, prompt_path=prompt_path)
    return 0


def doctor_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"{display_name()} doctor", description="Check local agent CLI availability.")
    parser.parse_args(sys.argv[1:] if argv is None else argv)

    status = 0
    checks = [
        ("codex", "agent"),
        ("claude", "agent"),
        ("gemini", "agent"),
        ("tmux", "farm"),
        ("codex-add", "farm launcher"),
    ]
    for name, role in checks:
        path = shutil.which(name)
        if path:
            print(f"ok: {name} ({role}) -> {path}")
        else:
            print(f"missing: {name} ({role})")
            if role == "agent":
                status = 1
    return status


def main(argv: list[str] | None = None) -> int:
    real_argv = list(sys.argv[1:] if argv is None else argv)
    default_agent = default_agent_from_invocation()
    if not real_argv:
        if (
            Path("agent-looper.toml").exists()
            or DEFAULT_SINGLE_PROMPT_FILE.exists()
            or DEFAULT_SEQUENCE_PROMPT_FILE.exists()
        ):
            return run_command_main([], default_agent=default_agent)
        return first_run_main()
    if real_argv[0] in {"-h", "--help"}:
        parser = argparse.ArgumentParser(prog=display_name(), description="Tiny coding-agent looper utility.")
        subparsers = parser.add_subparsers(dest="command")
        run_parser = subparsers.add_parser("run", help="run a prompt sequence loop")
        add_run_arguments(run_parser, default_agent=default_agent)
        subparsers.add_parser("init", help="create starter files")
        subparsers.add_parser("doctor", help="check local dependencies")
        parser.print_help()
        return 0

    command = real_argv[0]
    rest = real_argv[1:]
    if command == "run":
        return run_command_main(rest, default_agent=default_agent)
    if command == "init":
        return init_main(rest)
    if command == "doctor":
        return doctor_main(rest)

    return run_command_main(real_argv, default_agent=default_agent)


if __name__ == "__main__":
    raise SystemExit(main())
