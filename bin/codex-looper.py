#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from string import Formatter
from typing import Any

REPO_ROOT_FOR_IMPORTS = Path(__file__).resolve().parent.parent
if str(REPO_ROOT_FOR_IMPORTS) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT_FOR_IMPORTS))

from codex_looper.models import (
    CURRENT_LOG_POINTER_FILENAME,
    DEFAULT_COMPLETION_MARKER,
    DEFAULT_MAX_LOOPS,
    DEFAULT_MAX_TRANSIENT_RETRIES,
    DEFAULT_RETRY_NOTIFY_AFTER_SECONDS,
    DEFAULT_SEQUENCE_PROMPT_FILE,
    DEFAULT_SINGLE_PROMPT_FILE,
    DEFAULT_STOP_PATTERNS,
    DEFAULT_TIMEOUT_SECONDS,
    STREAM_READ_CHUNK_BYTES,
    TMUX_STATE_OPTION,
    TMUX_STOP_REASON_OPTION,
    VERSION,
    AgentConfig,
    CommandContext,
    CommandTemplateError,
    ConfigError,
    LoadedConfig,
    LooperConfig,
    LooperMode,
    ParsedLine,
    ProcessResult,
    PromptError,
    RunOptions,
    TmuxLayout,
)
from codex_looper.git_safety import (
    _run_git,
    create_backup_branch,
    git_workspace_fingerprint,
    prune_backup_branches,
)
from codex_looper.prompts import (
    load_prompts,
    load_prompts_for_mode,
    resolve_prompt_defaults,
)
from codex_looper.process import (
    _close_subprocess_transport,
    _safe_stream_write,
    _terminate_process_group,
    run_command as _run_command_impl,
)
from codex_looper.retry import (
    format_byte_count,
    format_duration,
    format_loop_metrics,
    is_retryable_stop_reason,
    parse_output_line,
    retry_delay_seconds,
    retry_notification_message,
    retry_status_message,
    should_notify_retry_wait,
    transient_retry_limit_message,
    transient_retry_limit_reached,
)
from codex_looper.tmux import (
    current_log_pointer_path,
    display_tmux_message,
    set_tmux_window_option,
    start_tmux_log_pane,
    tmux_log_tail_command,
    update_current_log_pointer,
)

__all__ = [
    "CURRENT_LOG_POINTER_FILENAME",
    "DEFAULT_COMPLETION_MARKER",
    "DEFAULT_MAX_LOOPS",
    "DEFAULT_MAX_TRANSIENT_RETRIES",
    "DEFAULT_RETRY_NOTIFY_AFTER_SECONDS",
    "DEFAULT_SEQUENCE_PROMPT_FILE",
    "DEFAULT_SINGLE_PROMPT_FILE",
    "DEFAULT_STOP_PATTERNS",
    "DEFAULT_TIMEOUT_SECONDS",
    "STREAM_READ_CHUNK_BYTES",
    "TMUX_STATE_OPTION",
    "TMUX_STOP_REASON_OPTION",
    "VERSION",
    "AgentConfig",
    "CommandContext",
    "CommandTemplateError",
    "ConfigError",
    "LoadedConfig",
    "LooperConfig",
    "LooperMode",
    "ParsedLine",
    "ProcessResult",
    "PromptError",
    "RunOptions",
    "TmuxLayout",
    "_close_subprocess_transport",
    "_run_git",
    "_safe_stream_write",
    "_terminate_process_group",
    "create_backup_branch",
    "current_log_pointer_path",
    "display_tmux_message",
    "format_byte_count",
    "format_duration",
    "format_loop_metrics",
    "git_workspace_fingerprint",
    "is_retryable_stop_reason",
    "load_config",
    "load_prompts",
    "load_prompts_for_mode",
    "parse_output_line",
    "prune_backup_branches",
    "resolve_prompt_defaults",
    "run_command",
    "run_command_main",
    "set_tmux_window_option",
    "start_tmux_log_pane",
    "tmux_log_tail_command",
    "update_current_log_pointer",
]

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 only.
    tomllib = None  # type: ignore[assignment]


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
    if not math.isfinite(out) or out <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return out


def nonnegative_float(value: str) -> float:
    out = float(value)
    if not math.isfinite(out) or out < 0:
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


def agent_extra_args(agent: AgentConfig) -> list[str]:
    args = list(agent.extra_args)
    if agent.model:
        args.extend(["--model", agent.model])
    if agent.effort:
        args.extend(["--effort", agent.effort])
    return args


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
        base.extend(agent_extra_args(agent))
        if is_first_prompt_in_session:
            return [*base, "--name", context.session, context.prompt]
        return [*base, "--resume", context.session, context.prompt]

    if agent.kind == "codex":
        base = ["codex", "exec", "--json"]
        base.extend(agent_extra_args(agent))
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
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error as exc:
            raise ConfigError(f"invalid stop pattern {pattern!r}: {exc}") from exc
    return compiled


def compile_completion_marker(looper: LooperConfig) -> re.Pattern[str] | None:
    if not looper.completion_enabled:
        return None
    try:
        return re.compile(looper.completion_marker)
    except re.error as exc:
        raise ConfigError(f"invalid completion marker {looper.completion_marker!r}: {exc}") from exc


UNCHECKED_MARKDOWN_TASK_PATTERN = re.compile(r"^\s*[-*]\s+\[\s\]", re.MULTILINE)


def markdown_plan_has_unchecked_tasks(text: str) -> bool:
    return bool(UNCHECKED_MARKDOWN_TASK_PATTERN.search(text))


def plan_file_all_tasks_checked(path: Path) -> bool:
    if not path.exists():
        raise ConfigError(f"plan file not found: {path}")
    return not markdown_plan_has_unchecked_tasks(path.read_text(encoding="utf-8"))


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
) -> ProcessResult:
    return await _run_command_impl(
        command=command,
        cwd=cwd,
        env=env,
        timeout_seconds=timeout_seconds,
        log_path=log_path,
        agent_kind=agent_kind,
        patterns=patterns,
        scan_stdout=scan_stdout,
        kill_on_stop_pattern=kill_on_stop_pattern,
        completion_pattern=completion_pattern,
        stream_output=stream_output,
        terminate_process_group=_terminate_process_group,
        close_subprocess_transport=_close_subprocess_transport,
        utc_stamp_fn=utc_stamp,
    )


def make_label(label: str | None, agent_name: str) -> str:
    if label:
        return label
    return f"Looper_{secrets.token_hex(3)}"


def make_run_dir(log_dir: Path, label: str) -> Path:
    safe_label = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in label).strip("-")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return log_dir / f"{stamp}__{safe_label or 'agent'}__{secrets.token_hex(3)}"


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
    if options.cb_output_decline is not None:
        updates["cb_output_decline"] = options.cb_output_decline
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

    tail_pane_active = start_tmux_log_pane(run_dir, options)
    set_tmux_window_option(TMUX_STATE_OPTION, "RUN")
    set_tmux_window_option(TMUX_STOP_REASON_OPTION, "")

    print(f"agent: {agent.name} ({agent.kind})")
    print(f"label: {label}")
    print(f"mode: {looper.mode}")
    print(f"prompts: {len(prompts)} from {looper.prompt_file}")
    print(f"logs: {run_dir}")
    print(
        "session mode: "
        + (
            "fresh session per loop"
            if looper.fresh_session_per_loop
            else "one session reused across loops"
        )
    )

    loop_number = 0
    completion_streak_count = 0
    no_progress_count = 0
    output_decline_count = 0
    previous_loop_output_bytes: int | None = None
    persistent_session_name = label
    persistent_session_id = ""
    fingerprint_ignored_paths = [run_dir.resolve()]

    while True:
        loop_number += 1
        loop_started_at = time.monotonic()
        loop_output_bytes = 0
        loop_completion_detected = False
        progress_before = None
        session_name = (
            f"{label}-loop-{loop_number:04d}"
            if looper.fresh_session_per_loop
            else persistent_session_name
        )
        session_id = "" if looper.fresh_session_per_loop else persistent_session_id
        first_prompt_in_session = looper.fresh_session_per_loop or loop_number == 1

        print(f"\n===== loop {loop_number} / session {session_name} =====")

        if looper.cb_no_progress:
            progress_before = git_workspace_fingerprint(
                agent.cwd, ignored_paths=fingerprint_ignored_paths
            )
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
            if tail_pane_active:
                update_current_log_pointer(run_dir=run_dir, log_path=log_path)

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
                    stream_output=not tail_pane_active,
                )

                if result.completion_detected:
                    loop_completion_detected = True
                loop_output_bytes += result.output_bytes

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
        loop_duration_seconds = time.monotonic() - loop_started_at
        print(
            format_loop_metrics(
                loop_number=loop_number,
                duration_seconds=loop_duration_seconds,
                output_bytes=loop_output_bytes,
            )
        )

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
            progress_after = git_workspace_fingerprint(
                agent.cwd, ignored_paths=fingerprint_ignored_paths
            )
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

        if looper.cb_output_decline:
            if (
                previous_loop_output_bytes is not None
                and loop_output_bytes < previous_loop_output_bytes
            ):
                output_decline_count += 1
                print(
                    "output declined "
                    f"({output_decline_count}/{looper.cb_output_decline}): "
                    f"{format_byte_count(previous_loop_output_bytes)} -> "
                    f"{format_byte_count(loop_output_bytes)}"
                )
                if output_decline_count >= looper.cb_output_decline:
                    reason = f"output declined for {output_decline_count} consecutive loop(s)"
                    print(f"STOP: {reason}")
                    set_tmux_window_option(TMUX_STATE_OPTION, "READY")
                    set_tmux_window_option(TMUX_STOP_REASON_OPTION, reason[:250])
                    return 0
            else:
                output_decline_count = 0
            previous_loop_output_bytes = loop_output_bytes

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


def _as_str(value: Any, key: str, default: str, *, nonempty: bool = False) -> str:
    if value is None:
        value = default
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string")
    if nonempty and not value:
        raise ConfigError(f"{key} must not be empty")
    return value


def _as_bool(value: Any, key: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be a boolean")
    return value


def _as_int(value: Any, key: str, default: int, *, minimum: int) -> int:
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer")
    if value < minimum:
        if minimum == 1:
            raise ConfigError(f"{key} must be greater than zero")
        raise ConfigError(f"{key} must be zero or greater")
    return value


def _as_float(value: Any, key: str, default: float, *, minimum: float, inclusive: bool) -> float:
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{key} must be a number")
    out = float(value)
    if not math.isfinite(out):
        raise ConfigError(f"{key} must be finite")
    if inclusive:
        if out < minimum:
            raise ConfigError(f"{key} must be {minimum:g} or greater")
    elif out <= minimum:
        raise ConfigError(f"{key} must be greater than {minimum:g}")
    return out


def _path(value: Any, default: Path) -> Path:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError("path values must be strings")
    return Path(value)


def _optional_path(value: Any, key: str) -> Path | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string path")
    return Path(value)


def _optional_str(value: Any, key: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string")
    return value


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
        try:
            if tomllib is None:
                return parse_basic_toml(path.read_text(encoding="utf-8"))
            else:
                with path.open("rb") as fh:
                    return tomllib.load(fh)
        except OSError as exc:
            raise ConfigError(f"failed to read config {path}: {exc}") from exc
        except Exception as exc:
            if exc.__class__.__name__ == "TOMLDecodeError":
                raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
            raise
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
    xdg_config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    candidates = [
        xdg_config_home / "codexfarm" / "presets" / name,
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
    raw_mode = _as_str(raw_looper.get("mode"), "looper.mode", default_looper.mode, nonempty=True)
    if raw_mode not in {"single", "sequence"}:
        raise ConfigError("looper.mode must be single or sequence")
    completion_streak = _as_int(
        raw_looper.get("completion_streak"),
        "looper.completion_streak",
        default_looper.completion_streak,
        minimum=1,
    )
    backup_keep = _as_int(
        raw_looper.get("backup_keep"),
        "looper.backup_keep",
        default_looper.backup_keep,
        minimum=0,
    )
    cb_no_progress = _as_int(
        raw_looper.get("cb_no_progress"),
        "looper.cb_no_progress",
        default_looper.cb_no_progress,
        minimum=0,
    )
    cb_output_decline = _as_int(
        raw_looper.get("cb_output_decline"),
        "looper.cb_output_decline",
        default_looper.cb_output_decline,
        minimum=0,
    )
    looper = LooperConfig(
        default_agent=_as_str(
            raw_looper.get("default_agent"),
            "looper.default_agent",
            default_looper.default_agent,
            nonempty=True,
        ),
        mode=raw_mode,  # type: ignore[arg-type]
        mode_explicit="mode" in raw_looper,
        prompt_file=_path(raw_looper.get("prompt_file"), default_looper.prompt_file),
        prompt_file_explicit="prompt_file" in raw_looper,
        separator=_as_str(
            raw_looper.get("separator"), "looper.separator", default_looper.separator
        ),
        timeout_seconds=_as_float(
            raw_looper.get("timeout_seconds"),
            "looper.timeout_seconds",
            default_looper.timeout_seconds,
            minimum=0,
            inclusive=False,
        ),
        sleep_seconds=_as_float(
            raw_looper.get("sleep_seconds"),
            "looper.sleep_seconds",
            default_looper.sleep_seconds,
            minimum=0,
            inclusive=False,
        ),
        fresh_session_per_loop=_as_bool(
            raw_looper.get("fresh_session_per_loop"),
            "looper.fresh_session_per_loop",
            default_looper.fresh_session_per_loop,
        ),
        max_loops=_as_int(
            raw_looper.get("max_loops"), "looper.max_loops", default_looper.max_loops, minimum=0
        ),
        max_transient_retries=_as_int(
            raw_looper.get("max_transient_retries"),
            "looper.max_transient_retries",
            default_looper.max_transient_retries,
            minimum=0,
        ),
        retry_notify_after_seconds=_as_float(
            raw_looper.get("retry_notify_after_seconds"),
            "looper.retry_notify_after_seconds",
            default_looper.retry_notify_after_seconds,
            minimum=0,
            inclusive=True,
        ),
        log_dir=_path(raw_looper.get("log_dir"), default_looper.log_dir),
        stop_patterns=_as_str_list(raw_looper.get("stop_patterns"), "looper.stop_patterns")
        or list(default_looper.stop_patterns),
        kill_on_stop_pattern=_as_bool(
            raw_looper.get("kill_on_stop_pattern"),
            "looper.kill_on_stop_pattern",
            default_looper.kill_on_stop_pattern,
        ),
        ignore_nonzero=_as_bool(
            raw_looper.get("ignore_nonzero"), "looper.ignore_nonzero", default_looper.ignore_nonzero
        ),
        scan_stdout_for_stop_patterns=_as_bool(
            raw_looper.get(
                "scan_stdout_for_stop_patterns",
            ),
            "looper.scan_stdout_for_stop_patterns",
            default_looper.scan_stdout_for_stop_patterns,
        ),
        completion_enabled=_as_bool(
            raw_looper.get("completion_enabled"),
            "looper.completion_enabled",
            default_looper.completion_enabled,
        ),
        completion_marker=_as_str(
            raw_looper.get("completion_marker"),
            "looper.completion_marker",
            default_looper.completion_marker,
            nonempty=True,
        ),
        completion_streak=completion_streak,
        plan_file=_optional_path(raw_looper.get("plan_file"), "looper.plan_file"),
        backup_enabled=_as_bool(
            raw_looper.get("backup_enabled"), "looper.backup_enabled", default_looper.backup_enabled
        ),
        backup_prefix=_as_str(
            raw_looper.get("backup_prefix"),
            "looper.backup_prefix",
            default_looper.backup_prefix,
            nonempty=True,
        ),
        backup_keep=backup_keep,
        cb_no_progress=cb_no_progress,
        cb_output_decline=cb_output_decline,
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
        kind = _as_str(
            value.get("kind"), f"agents.{name}.kind", base.kind if base else name, nonempty=True
        )
        if kind not in {"claude", "codex", "generic"}:
            raise ConfigError(f"agents.{name}.kind must be claude, codex, or generic")
        agents[name] = AgentConfig(
            name=name,
            kind=kind,  # type: ignore[arg-type]
            cwd=_path(value.get("cwd"), base.cwd if base else Path(".")),
            extra_args=_as_str_list(value.get("extra_args"), f"agents.{name}.extra_args")
            or (base.extra_args if base else []),
            model=_optional_str(value.get("model"), f"agents.{name}.model")
            or (base.model if base else None),
            effort=_optional_str(value.get("effort"), f"agents.{name}.effort")
            or (base.effort if base else None),
            first_command=(
                _as_str_list(value.get("first_command"), f"agents.{name}.first_command")
                or (base.first_command if base else None)
            ),
            resume_command=(
                _as_str_list(value.get("resume_command"), f"agents.{name}.resume_command")
                or (base.resume_command if base else None)
            ),
            env={
                **(base.env if base else {}),
                **_as_str_dict(value.get("env"), f"agents.{name}.env"),
            },
            scan_stdout_for_stop_patterns=_as_bool(
                value.get("scan_stdout_for_stop_patterns"),
                f"agents.{name}.scan_stdout_for_stop_patterns",
                base.scan_stdout_for_stop_patterns if base else False,
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
# Completion detection is opt-in. Agents should emit the marker only when done.
completion_enabled = false
completion_marker = "EXIT_SIGNAL:\\\\s*true"
completion_streak = 1
# Optional markdown checklist gate for completion.
plan_file = ""
# Optional git safety controls.
backup_enabled = false
backup_prefix = "looper-backup"
backup_keep = 10
cb_no_progress = 0
cb_output_decline = 0

[agents.claude]
kind = "claude"
# extra_args are inserted into the built-in Claude Code command templates.
# Optional sugar:
# model = "claude-opus-4-8"
# effort = "max"
# Example:
# extra_args = ["--permission-mode", "acceptEdits", "--max-turns", "20"]
# For isolated unattended sandboxes, Claude also supports:
# extra_args = ["--dangerously-skip-permissions"]
extra_args = []

[agents.codex]
kind = "codex"
# Optional sugar:
# model = "gpt-5.4"
# effort = "high"
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


def guidance_lines(
    *, initialized: bool, prompt_path: Path = DEFAULT_SINGLE_PROMPT_FILE
) -> list[str]:
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


def default_tmux_layout_from_env() -> TmuxLayout:
    raw = os.environ.get("CODEX_LOOPER_LAYOUT", "auto").strip().lower()
    if raw in {"auto", "single", "split"}:
        return raw  # type: ignore[return-value]
    return "auto"


def resolve_self_executable() -> str:
    configured = os.environ.get("CODEX_LOOPER_COMMAND")
    if configured:
        return str(Path(configured).expanduser().resolve())

    argv0 = Path(sys.argv[0]).expanduser()
    if argv0.is_absolute() or argv0.parent != Path("."):
        return str(argv0.resolve())

    executable = shutil.which(argv0.name) or str(argv0)
    return str(Path(executable).resolve())


def maybe_launch_farm(options: RunOptions, original_argv: list[str]) -> int | None:
    if options.local or options.farm_session is None:
        return None
    launcher = shutil.which(options.farm_add_bin)
    if launcher is None:
        raise ConfigError(f"farm launcher not found: {options.farm_add_bin}")

    executable = resolve_self_executable()
    cwd = options.cwd or Path.cwd()
    env = os.environ.copy()
    env["CODEX_NAME"] = env.get("CODEX_NAME") or cwd.name
    env["CODEX_CMD"] = executable
    inner_args = clean_farm_args(original_argv)
    layout_arg_present = _argv_has_option(inner_args, "--tmux-layout")
    propagated_layout = (
        options.tmux_layout if options.tmux_layout != "auto" or layout_arg_present else "split"
    )
    env["CODEX_LOOPER_LAYOUT"] = env.get("CODEX_LOOPER_LAYOUT") or propagated_layout
    if not layout_arg_present:
        inner_args.extend(["--tmux-layout", propagated_layout])
    if not _argv_has_option(inner_args, "--local"):
        inner_args.append("--local")
    if options.label and not _argv_has_option(inner_args, "--label", "-l"):
        inner_args.extend(["--label", options.label])
    env["CODEX_ARGS"] = shlex.join(inner_args)

    command = [launcher]
    if not options.farm_attach:
        command.append("-d")
    if options.farm_session:
        command.append(options.farm_session)
    command.append(str(cwd))
    try:
        return subprocess.run(command, env=env, check=False).returncode
    except PermissionError as exc:
        raise ConfigError(f"farm launcher is not runnable: {options.farm_add_bin}") from exc


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
    parser.add_argument(
        "-l", "--label", help="human-readable run/session/log label; does not rename tmux windows"
    )
    parser.add_argument(
        "--tmux-layout",
        choices=("auto", "single", "split"),
        default=default_tmux_layout_from_env(),
        help="tmux layout for local execution: auto, single, or split (default from CODEX_LOOPER_LAYOUT or auto)",
    )
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
    parser.add_argument(
        "--backup", dest="backup_enabled", action="store_true", help="create git backup branches"
    )
    parser.add_argument("--backup-prefix", help="git branch prefix for backup branches")
    parser.add_argument(
        "--backup-keep", type=nonnegative_int, help="number of newest backup branches to keep"
    )
    parser.add_argument(
        "--cb-no-progress",
        type=nonnegative_int,
        help="stop after this many completed loops with no git workspace change; 0 disables",
    )
    parser.add_argument(
        "--cb-output-decline",
        type=nonnegative_int,
        help="stop after this many consecutive completed loops with declining output bytes; 0 disables",
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
    parser.add_argument(
        "--dry-run", action="store_true", help="print commands without running them"
    )
    parser.add_argument(
        "--ignore-nonzero", action="store_true", default=None, help="do not stop on nonzero exit"
    )
    parser.add_argument(
        "--stop-on-nonzero",
        dest="ignore_nonzero",
        action="store_false",
        help="stop on nonzero exit",
    )
    parser.add_argument(
        "--hold-on-stop", action="store_true", help="wait for Enter before exiting after a stop"
    )
    parser.add_argument(
        "--local", action="store_true", help="run in this terminal instead of the default farm"
    )
    parser.add_argument(
        "--farm-session",
        nargs="?",
        const="",
        help="launch this looper in a Codex CLI Farm tmux session; omit NAME to use the default",
    )
    parser.add_argument(
        "--farm-attach", action="store_true", help="attach after launching with --farm-session"
    )
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


def parse_run_options(
    args: argparse.Namespace, *, agent_args: list[str] | None = None
) -> RunOptions:
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
        cb_output_decline=args.cb_output_decline,
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
        tmux_layout=args.tmux_layout,
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

    try:
        farm_result = maybe_launch_farm(options, real_argv)
        if farm_result is not None:
            return farm_result

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
    parser = argparse.ArgumentParser(
        prog=f"{display_name()} init", description="Create starter config and prompt files."
    )
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    parser.add_argument(
        "-i", "--interactive", action="store_true", help="ask for defaults and prompt text"
    )
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
    parser = argparse.ArgumentParser(
        prog=f"{display_name()} doctor", description="Check local agent CLI availability."
    )
    parser.add_argument(
        "-a", "--agent", default=default_agent_from_invocation(), help="agent executable to check"
    )
    parser.add_argument("--local", action="store_true", help="skip tmux and farm launcher checks")
    parser.add_argument("--farm-add-bin", default="codex-add", help="farm launcher to check")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    status = 0
    checks = [(args.agent, "agent")]
    if not args.local:
        checks.extend([("tmux", "farm"), (args.farm_add_bin, "farm launcher")])
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
        parser = argparse.ArgumentParser(
            prog=display_name(),
            description=(
                "Tiny coding-agent looper utility. The run subcommand is optional; "
                "initialized directories run when no subcommand is given."
            ),
        )
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
