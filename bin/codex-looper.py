#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from string import Formatter
from typing import Any, Literal, TextIO

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 only.
    tomllib = None  # type: ignore[assignment]


VERSION = "0.1.0"
AgentKind = Literal["claude", "codex", "generic"]
TMUX_STATE_OPTION = "@codex_state"
TMUX_STOP_REASON_OPTION = "@codex_stop_reason"

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
    prompt_file: Path = Path("prompts.md")
    separator: str = r"^---\s*$"
    timeout_seconds: float = 900.0
    sleep_seconds: float = 2.0
    fresh_session_per_loop: bool = True
    max_loops: int = 0
    log_dir: Path = Path(".agent-looper/runs")
    stop_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_STOP_PATTERNS))
    kill_on_stop_pattern: bool = True
    ignore_nonzero: bool = False
    scan_stdout_for_stop_patterns: bool = False


@dataclass(frozen=True)
class LoadedConfig:
    looper: LooperConfig
    agents: dict[str, AgentConfig]


@dataclass(frozen=True)
class RunOptions:
    agent_name: str | None
    config_path: Path
    prompt_file: Path | None = None
    label: str | None = None
    timeout_seconds: float | None = None
    sleep_seconds: float | None = None
    max_loops: int | None = None
    once: bool = False
    fresh_session_per_loop: bool | None = None
    cwd: Path | None = None
    dry_run: bool = False
    ignore_nonzero: bool | None = None
    hold_on_stop: bool = False
    farm_session: str | None = None
    farm_attach: bool = False
    farm_add_bin: str = "codex-add"


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


@dataclass
class ProcessResult:
    returncode: int | None
    session_id: str | None = None
    stop_reason: str | None = None
    timed_out: bool = False


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


def nonnegative_int(value: str) -> int:
    out = int(value)
    if out < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
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


def parse_output_line(
    *,
    line: str,
    stream: str,
    agent_kind: str,
    patterns: list[re.Pattern[str]],
    scan_stdout: bool,
) -> ParsedLine:
    stripped = line.strip()
    parsed = ParsedLine()

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
            if status in {"allowed_warning", "rejected"} or status is None:
                parsed.stop_reason = f"rate limit event: status={status or 'unknown'}"
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
                return parsed

        return parsed

    if stream == "stderr" or scan_stdout:
        match = _match_text(line, patterns)
        if match:
            parsed.stop_reason = f"stop pattern detected in {stream}: {match!r}"

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
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{agent_name}-{stamp}"


def make_run_dir(log_dir: Path, label: str) -> Path:
    safe_label = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in label).strip("-")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return log_dir / f"{stamp}__{safe_label or 'agent'}"


def apply_run_options(config: LooperConfig, options: RunOptions) -> LooperConfig:
    updates: dict[str, Any] = {}
    if options.prompt_file is not None:
        updates["prompt_file"] = options.prompt_file
    if options.timeout_seconds is not None:
        updates["timeout_seconds"] = options.timeout_seconds
    if options.sleep_seconds is not None:
        updates["sleep_seconds"] = options.sleep_seconds
    if options.max_loops is not None:
        updates["max_loops"] = options.max_loops
    if options.once:
        updates["max_loops"] = 1
    if options.fresh_session_per_loop is not None:
        updates["fresh_session_per_loop"] = options.fresh_session_per_loop
    if options.ignore_nonzero is not None:
        updates["ignore_nonzero"] = options.ignore_nonzero
    return replace(config, **updates)


async def run_loop(*, agent: AgentConfig, looper: LooperConfig, options: RunOptions) -> int:
    label = make_label(options.label, options.agent_name)
    run_dir = make_run_dir(looper.log_dir, label)
    run_dir.mkdir(parents=True, exist_ok=True)

    prompts = load_prompts(looper.prompt_file, looper.separator)
    patterns = compile_stop_patterns(looper.stop_patterns)

    if options.cwd is not None:
        agent = replace(agent, cwd=options.cwd)

    if not agent.cwd.exists():
        print(f"agent cwd does not exist: {agent.cwd}", file=sys.stderr)
        return 2

    rename_current_window(label)
    set_tmux_window_option(TMUX_STATE_OPTION, "RUN")
    set_tmux_window_option(TMUX_STOP_REASON_OPTION, "")

    print(f"agent: {agent.name} ({agent.kind})")
    print(f"label: {label}")
    print(f"prompts: {len(prompts)} from {looper.prompt_file}")
    print(f"logs: {run_dir}")
    print(
        "session mode: "
        + ("fresh session per loop" if looper.fresh_session_per_loop else "one session reused across loops")
    )

    loop_number = 0
    persistent_session_name = label
    persistent_session_id = ""

    while True:
        loop_number += 1
        session_name = (
            f"{label}-loop-{loop_number:04d}" if looper.fresh_session_per_loop else persistent_session_name
        )
        session_id = "" if looper.fresh_session_per_loop else persistent_session_id
        first_prompt_in_session = looper.fresh_session_per_loop or loop_number == 1

        print(f"\n===== loop {loop_number} / session {session_name} =====")

        for prompt_index, prompt in enumerate(prompts, start=1):
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
                is_first_prompt_in_session=first_prompt_in_session,
            )
            first_prompt_in_session = False
            log_path = run_dir / f"loop-{loop_number:04d}__prompt-{prompt_index:03d}.log"

            print(f"\n--- prompt {prompt_index}/{len(prompts)} ---")
            print(f"$ {shlex.join(command)}")

            if options.dry_run:
                continue

            result = await run_command(
                command=command,
                cwd=agent.cwd,
                env=agent.env,
                timeout_seconds=looper.timeout_seconds,
                log_path=log_path,
                agent_kind=agent.kind,
                patterns=patterns,
                scan_stdout=(looper.scan_stdout_for_stop_patterns or agent.scan_stdout_for_stop_patterns),
                kill_on_stop_pattern=looper.kill_on_stop_pattern,
            )

            if result.session_id:
                session_id = result.session_id
                if not looper.fresh_session_per_loop:
                    persistent_session_id = session_id

            if result.stop_reason:
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

        print(f"\ncompleted loop {loop_number}")

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


def load_config(path: Path) -> LoadedConfig:
    if path.exists():
        if tomllib is None:
            raw = parse_basic_toml(path.read_text(encoding="utf-8"))
        else:
            with path.open("rb") as fh:
                raw = tomllib.load(fh)
    else:
        raw = {}

    raw_looper = raw.get("looper", {})
    if raw_looper is None:
        raw_looper = {}
    if not isinstance(raw_looper, dict):
        raise ConfigError("[looper] must be a TOML table")

    default_looper = LooperConfig()
    looper = LooperConfig(
        default_agent=str(raw_looper.get("default_agent", default_looper.default_agent)),
        prompt_file=_path(raw_looper.get("prompt_file"), default_looper.prompt_file),
        separator=str(raw_looper.get("separator", default_looper.separator)),
        timeout_seconds=float(raw_looper.get("timeout_seconds", default_looper.timeout_seconds)),
        sleep_seconds=float(raw_looper.get("sleep_seconds", default_looper.sleep_seconds)),
        fresh_session_per_loop=bool(
            raw_looper.get("fresh_session_per_loop", default_looper.fresh_session_per_loop)
        ),
        max_loops=int(raw_looper.get("max_loops", default_looper.max_loops)),
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
    timeout_seconds: str = "900",
    sleep_seconds: str = "2",
) -> str:
    return f"""# agent-looper.toml
# Prompt files split sequences with a line containing only ---.

[looper]
default_agent = {json.dumps(default_agent)}
prompt_file = "prompts.md"
timeout_seconds = {timeout_seconds}
sleep_seconds = {sleep_seconds}
fresh_session_per_loop = true
# max_loops = 0 means forever. Use --once or --max-loops for a bounded run.
max_loops = 0
log_dir = ".agent-looper/runs"

[agents.claude]
kind = "claude"
# extra_args are inserted into the built-in Claude Code command templates.
# Example:
# extra_args = ["--permission-mode", "auto", "--max-turns", "20"]
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


EXAMPLE_PROMPTS = """Summarize the repository layout. Do not modify files.
---
Identify the smallest useful task to improve the project. Do not modify files.
---
If the previous task is safe and obvious, implement it. Then run the most relevant fast check.
"""


def write_starter_files(
    *,
    force: bool,
    config_text: str = EXAMPLE_CONFIG,
    prompts_text: str = EXAMPLE_PROMPTS,
) -> list[Path]:
    files = [
        (Path("agent-looper.toml"), config_text),
        (Path("prompts.md"), prompts_text),
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


def guidance_lines(*, initialized: bool) -> list[str]:
    heading = (
        "Initialized Agent Looper starter files."
        if initialized
        else "Agent Looper is already initialized."
    )
    return [
        heading,
        "",
        "Next steps:",
        "  1. Edit prompts.md; split prompts with a line containing only ---",
        "  2. Try a bounded run: codex-looper --once --label smoke",
        "  3. Run in tmux farm: codex-looper --farm-session work --label sweep",
        "",
        "Customize defaults with: codex-looper init --interactive --force",
    ]


def print_guidance(*, initialized: bool) -> None:
    print("\n".join(guidance_lines(initialized=initialized)))


def first_run_main() -> int:
    config = Path("agent-looper.toml")
    prompts = Path("prompts.md")
    already_initialized = config.exists() and prompts.exists()
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


def interactive_starter_content() -> tuple[str, str]:
    print("Agent Looper interactive setup")
    default_agent = _ask("Default agent (codex, claude, gemini)", "codex")
    if default_agent not in default_agents():
        print(f"Unknown default agent {default_agent!r}; using codex.")
        default_agent = "codex"
    timeout_seconds = _ask_number("Timeout seconds per prompt", "900")
    sleep_seconds = _ask_number("Sleep seconds between loops", "2")

    print("Enter prompts one at a time. Submit a blank prompt when finished.")
    prompts: list[str] = []
    while True:
        prompt = input(f"Prompt {len(prompts) + 1}: ").strip()
        if not prompt:
            break
        prompts.append(prompt)
    prompt_text = "\n---\n".join(prompts).strip() + "\n" if prompts else EXAMPLE_PROMPTS
    return (
        build_example_config(
            default_agent=default_agent,
            timeout_seconds=timeout_seconds,
            sleep_seconds=sleep_seconds,
        ),
        prompt_text,
    )


def clean_farm_args(argv: list[str]) -> list[str]:
    cleaned: list[str] = []
    skip_next = False
    for item in argv:
        if skip_next:
            skip_next = False
            continue
        if item in {"--farm-session", "--farm-add-bin"}:
            skip_next = True
            continue
        if item.startswith("--farm-session=") or item.startswith("--farm-add-bin="):
            continue
        if item == "--farm-attach":
            continue
        cleaned.append(item)
    return cleaned


def maybe_launch_farm(options: RunOptions, original_argv: list[str]) -> int | None:
    if not options.farm_session:
        return None

    executable = os.environ.get("CODEX_LOOPER_COMMAND") or shutil.which(Path(sys.argv[0]).name) or sys.argv[0]
    executable = str(Path(executable).resolve())
    cwd = options.cwd or Path.cwd()
    env = os.environ.copy()
    env["CODEX_NAME"] = options.label or cwd.name
    env["CODEX_CMD"] = executable
    env["CODEX_ARGS"] = shlex.join(clean_farm_args(original_argv))

    command = [options.farm_add_bin]
    if not options.farm_attach:
        command.append("-d")
    command.extend([options.farm_session, str(cwd)])
    return subprocess.run(command, env=env, check=False).returncode


def add_run_arguments(parser: argparse.ArgumentParser, *, default_agent: str | None = None) -> None:
    parser.add_argument("-a", "--agent", default=None, help="agent config name")
    parser.add_argument("-c", "--config", default="agent-looper.toml", help="config file path")
    parser.add_argument("-p", "--prompt-file", help="prompt sequence file")
    parser.add_argument("-l", "--label", help="human-readable run label; also used for resumable sessions")
    parser.add_argument("--timeout", type=positive_float, help="timeout seconds per prompt")
    parser.add_argument("--sleep", type=positive_float, help="sleep seconds between loops")
    parser.add_argument("--max-loops", type=nonnegative_int, help="0 means forever")
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
    parser.add_argument("--farm-session", help="launch this looper in a Codex CLI Farm tmux session")
    parser.add_argument("--farm-attach", action="store_true", help="attach after launching with --farm-session")
    parser.add_argument("--farm-add-bin", default="codex-add", help="codex-add-compatible launcher")
    parser.add_argument("--version", action="version", version=f"codex-looper {VERSION}")


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


def parse_run_options(args: argparse.Namespace) -> RunOptions:
    return RunOptions(
        agent_name=args.agent,
        config_path=Path(args.config),
        prompt_file=Path(args.prompt_file) if args.prompt_file else None,
        label=args.label,
        timeout_seconds=args.timeout,
        sleep_seconds=args.sleep,
        max_loops=args.max_loops,
        once=args.once,
        fresh_session_per_loop=args.fresh_session_per_loop,
        cwd=Path(args.cwd) if args.cwd else None,
        dry_run=args.dry_run,
        ignore_nonzero=args.ignore_nonzero,
        hold_on_stop=args.hold_on_stop,
        farm_session=args.farm_session,
        farm_attach=args.farm_attach,
        farm_add_bin=args.farm_add_bin,
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
    parser = argparse.ArgumentParser(
        prog=display_name(),
        description="Loop a coding-agent prompt sequence until timeout, rate-limit, or backoff signals appear.",
    )
    add_run_arguments(parser, default_agent=default_agent)
    args = parser.parse_args(real_argv)
    options = parse_run_options(args)

    farm_result = maybe_launch_farm(options, real_argv)
    if farm_result is not None:
        return farm_result

    try:
        loaded = load_config(options.config_path)
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
    if args.interactive:
        config_text, prompts_text = interactive_starter_content()

    write_starter_files(force=args.force, config_text=config_text, prompts_text=prompts_text)
    print("")
    print("Starter files are ready.")
    print_guidance(initialized=True)
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
