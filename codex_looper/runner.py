from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
import shlex
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agents import build_command
from .control import ControlCommand, control_file_path, read_control_commands
from .git_safety import create_backup_branch, git_workspace_fingerprint, prune_backup_branches
from .hybrid import ClaudeHybridController, build_claude_hybrid_command
from .models import (
    TMUX_STATE_OPTION,
    TMUX_STOP_REASON_OPTION,
    AgentConfig,
    CommandContext,
    ConfigError,
    LooperConfig,
    ProcessResult,
    PromptError,
    RunOptions,
)
from .process import (
    _close_subprocess_transport,
    _terminate_process_group,
)
from .process import (
    run_command as _run_command_impl,
)
from .prompts import load_prompts_for_mode, resolve_prompt_defaults
from .retry import (
    format_byte_count,
    format_duration,
    format_loop_metrics,
    is_retryable_stop_reason,
    retry_delay_seconds,
    retry_notification_message,
    retry_status_message,
    should_notify_retry_wait,
    transient_retry_limit_message,
    transient_retry_limit_reached,
)
from .state import LooperStateRecorder
from .tmux import (
    display_tmux_message,
    set_tmux_window_option,
    start_tmux_log_pane,
    update_current_log_pointer,
)

RunCommandFn = Callable[..., Awaitable[ProcessResult]]
RunClaudeHybridTurnFn = Callable[..., Awaitable[ProcessResult]]
SetTmuxWindowOptionFn = Callable[[str, str], None]
DisplayTmuxMessageFn = Callable[[str], None]
StartTmuxLogPaneFn = Callable[[Path, RunOptions], bool]
UpdateCurrentLogPointerFn = Callable[..., None]
SleepFn = Callable[[float], Awaitable[None]]


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


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


def compile_output_match_pattern(looper: LooperConfig) -> re.Pattern[str] | None:
    if not looper.cb_output_match_pattern:
        return None
    try:
        return re.compile(looper.cb_output_match_pattern)
    except re.error as exc:
        raise ConfigError(
            f"invalid cb_output_match_pattern {looper.cb_output_match_pattern!r}: {exc}"
        ) from exc


def loop_logs_match(pattern: re.Pattern[str], paths: list[Path]) -> bool:
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if pattern.search(text):
            return True
    return False


UNCHECKED_MARKDOWN_TASK_PATTERN = re.compile(r"^\s*[-*]\s+\[\s\]", re.MULTILINE)


def markdown_plan_has_unchecked_tasks(text: str) -> bool:
    return bool(UNCHECKED_MARKDOWN_TASK_PATTERN.search(text))


def plan_file_all_tasks_checked(path: Path) -> bool:
    if not path.exists():
        raise ConfigError(f"plan file not found: {path}")
    return not markdown_plan_has_unchecked_tasks(path.read_text(encoding="utf-8"))


def prompt_file_state(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    stat = path.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
    return {
        "prompt_sha256": hashlib.sha256(data).hexdigest(),
        "prompt_mtime": mtime.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "prompt_bytes": len(data),
    }


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
    on_process_started: Callable[[int, int | None], None] | None = None,
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
        on_process_started=on_process_started,
    )


async def run_claude_hybrid_turn(
    *,
    controller: ClaudeHybridController,
    prompt: str,
    timeout_seconds: float,
    log_path: Path,
    patterns: list[re.Pattern[str]],
    completion_pattern: re.Pattern[str] | None,
    session_name: str,
    session_id: str,
) -> ProcessResult:
    del session_name, session_id
    return await asyncio.to_thread(
        controller.run_turn,
        prompt=prompt,
        timeout_seconds=timeout_seconds,
        log_path=log_path,
        completion_pattern=completion_pattern,
        stop_patterns=patterns,
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
    if options.cb_output_match_pattern is not None:
        updates["cb_output_match_pattern"] = options.cb_output_match_pattern
    if options.cb_output_match_repeats is not None:
        updates["cb_output_match_repeats"] = options.cb_output_match_repeats
    if options.once:
        updates["max_loops"] = 1
    if options.fresh_session_per_loop is not None:
        updates["fresh_session_per_loop"] = options.fresh_session_per_loop
    if options.ignore_nonzero is not None:
        updates["ignore_nonzero"] = options.ignore_nonzero
    return replace(config, **updates)


async def run_loop(
    *,
    agent: AgentConfig,
    looper: LooperConfig,
    options: RunOptions,
    run_command_fn: RunCommandFn = run_command,
    run_claude_hybrid_turn_fn: RunClaudeHybridTurnFn = run_claude_hybrid_turn,
    set_tmux_window_option_fn: SetTmuxWindowOptionFn = set_tmux_window_option,
    display_tmux_message_fn: DisplayTmuxMessageFn = display_tmux_message,
    start_tmux_log_pane_fn: StartTmuxLogPaneFn = start_tmux_log_pane,
    update_current_log_pointer_fn: UpdateCurrentLogPointerFn = update_current_log_pointer,
    sleep_fn: SleepFn = asyncio.sleep,
) -> int:
    looper = resolve_prompt_defaults(looper, cwd=Path.cwd())
    label = make_label(options.label, options.agent_name)
    run_dir = make_run_dir(looper.log_dir, label)
    run_dir.mkdir(parents=True, exist_ok=True)

    def load_current_prompts() -> list[str]:
        return load_prompts_for_mode(looper.prompt_file, looper.separator, looper.mode)

    prompts = load_current_prompts()
    prompt_reload_count = 1
    prompt_state = prompt_file_state(looper.prompt_file)
    patterns = compile_stop_patterns(looper.stop_patterns)
    completion_pattern = compile_completion_marker(looper)
    output_match_pattern = compile_output_match_pattern(looper)

    if options.cwd is not None:
        agent = replace(agent, cwd=options.cwd)

    if not agent.cwd.exists():
        print(f"agent cwd does not exist: {agent.cwd}", file=sys.stderr)
        return 2

    if options.agent_args:
        agent = replace(agent, extra_args=[*agent.extra_args, *options.agent_args])

    use_claude_hybrid = agent.interface == "hybrid"
    if use_claude_hybrid and agent.kind != "claude":
        raise ConfigError("hybrid interface is currently only supported for claude agents")
    claude_hybrid_controller = (
        ClaudeHybridController(
            command=build_claude_hybrid_command(agent),
            cwd=agent.cwd,
            env=agent.env,
        )
        if use_claude_hybrid
        else None
    )

    state = LooperStateRecorder(
        run_dir,
        {
            "run_id": run_dir.name,
            "run_dir": str(run_dir),
            "label": label,
            "agent_name": agent.name,
            "agent_kind": agent.kind,
            "agent_interface": agent.interface,
            "agent_cwd": str(agent.cwd),
            "config_path": str(options.config_path),
            "prompt_file": str(looper.prompt_file),
            **prompt_state,
            "prompt_reload_count": prompt_reload_count,
            "mode": looper.mode,
            "reload_prompt_each_loop": looper.reload_prompt_each_loop,
            "control_file": str(control_file_path(run_dir)),
            "control_processed_count": 0,
            "control_last_action": None,
            "control_last_reason": None,
            "control_last_id": None,
            "pid": os.getpid(),
            "child_pid": None,
            "child_pgid": None,
            "hybrid_pane_id": None,
            "hybrid_pane_pid": None,
            "status": "running",
            "total_prompts": len(prompts),
            "current_loop": 0,
            "current_prompt_index": 0,
            "current_session": "",
            "current_session_id": "",
            "last_log": None,
            "last_output_bytes": 0,
            "stop_reason": None,
            "exit_code": None,
        },
    )
    state.record("run_started", status="running")

    processed_control_ids: set[str] = set()
    control_stop_after_prompt: ControlCommand | None = None
    control_stop_after_loop: ControlCommand | None = None
    control_interrupt_now: ControlCommand | None = None

    def control_stop_reason(command: ControlCommand) -> str:
        reason = command.reason.strip()
        if reason:
            return f"control {command.action}: {reason}"
        return f"control {command.action}"

    def poll_control_commands() -> None:
        nonlocal control_interrupt_now, control_stop_after_loop, control_stop_after_prompt
        for command in read_control_commands(run_dir, processed_ids=processed_control_ids):
            processed_control_ids.add(command.id)
            state.record(
                "control_command_received",
                control_last_action=command.action,
                control_last_reason=command.reason,
                control_last_id=command.id,
                control_processed_count=len(processed_control_ids),
            )
            if command.action == "stop_after_prompt":
                control_stop_after_prompt = command
            elif command.action == "stop_after_loop":
                control_stop_after_loop = command
            elif command.action == "interrupt_now":
                control_interrupt_now = command

    def next_control_prompt_stop() -> ControlCommand | None:
        return control_interrupt_now or control_stop_after_prompt

    def stop_for_control(command: ControlCommand) -> int:
        reason = control_stop_reason(command)
        print(f"\nSTOP: {reason}")
        set_tmux_window_option_fn(TMUX_STATE_OPTION, "READY")
        set_tmux_window_option_fn(TMUX_STOP_REASON_OPTION, reason[:250])
        return stop_run(reason=reason, exit_code=0)

    def record_child_process(pid: int, pgid: int | None) -> None:
        state.update(child_pid=pid, child_pgid=pgid)

    def stop_run(*, reason: str, exit_code: int, status: str = "stopped") -> int:
        state.record(
            "run_stopped",
            status=status,
            stop_reason=reason,
            exit_code=exit_code,
        )
        return exit_code

    tail_pane_active = False if use_claude_hybrid else start_tmux_log_pane_fn(run_dir, options)
    set_tmux_window_option_fn(TMUX_STATE_OPTION, "RUN")
    set_tmux_window_option_fn(TMUX_STOP_REASON_OPTION, "")

    print(f"agent: {agent.name} ({agent.kind})")
    print(f"interface: {agent.interface}")
    print(f"label: {label}")
    print(f"mode: {looper.mode}")
    print(f"prompts: {len(prompts)} from {looper.prompt_file}")
    print(
        "prompt reload: "
        + ("before each loop" if looper.reload_prompt_each_loop else "startup only")
    )
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
    output_match_count = 0
    previous_loop_output_bytes: int | None = None
    persistent_session_name = label
    persistent_session_id = ""
    fingerprint_ignored_paths = [looper.log_dir.resolve(), run_dir.resolve()]

    while True:
        poll_control_commands()
        if control_stop := (next_control_prompt_stop() or control_stop_after_loop):
            return stop_for_control(control_stop)

        loop_number += 1
        if looper.reload_prompt_each_loop:
            try:
                prompts = load_current_prompts()
                prompt_reload_count += 1
                state.update(
                    **prompt_file_state(looper.prompt_file),
                    prompt_reload_count=prompt_reload_count,
                )
            except (ConfigError, PromptError) as exc:
                reason = f"prompt reload failed: {exc}"
                print(f"\nSTOP: {reason}", file=sys.stderr)
                set_tmux_window_option_fn(TMUX_STATE_OPTION, "ERR")
                set_tmux_window_option_fn(TMUX_STOP_REASON_OPTION, reason[:250])
                return stop_run(reason=reason, exit_code=2, status="error")

        loop_started_at = time.monotonic()
        loop_output_bytes = 0
        loop_log_paths: list[Path] = []
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
        state.record(
            "loop_started",
            status="running",
            current_loop=loop_number,
            current_prompt_index=0,
            current_session=session_name,
            current_session_id=session_id,
            total_prompts=len(prompts),
            stop_reason=None,
        )

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
            loop_log_paths.append(log_path)
            if tail_pane_active:
                update_current_log_pointer_fn(run_dir=run_dir, log_path=log_path)

            while True:
                state.record(
                    "prompt_started",
                    status="running",
                    current_loop=loop_number,
                    current_prompt_index=prompt_index,
                    current_session=session_name,
                    current_session_id=session_id,
                    last_log=str(log_path),
                    retry_count=retry_count,
                    stop_reason=None,
                )
                context = CommandContext(
                    prompt=prompt,
                    session=session_name,
                    session_id=session_id,
                    loop=loop_number,
                    prompt_index=prompt_index,
                    label=label,
                    run_dir=run_dir,
                )
                if use_claude_hybrid:
                    command = build_claude_hybrid_command(agent)
                else:
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
                    state.record(
                        "prompt_completed",
                        status="running",
                        current_session_id=session_id,
                        returncode=0,
                        last_output_bytes=0,
                        dry_run=True,
                    )
                    break

                if use_claude_hybrid:
                    if claude_hybrid_controller is None:
                        raise ConfigError("Claude hybrid controller was not initialized")
                    result = await run_claude_hybrid_turn_fn(
                        controller=claude_hybrid_controller,
                        prompt=prompt,
                        timeout_seconds=looper.timeout_seconds,
                        log_path=log_path,
                        patterns=patterns,
                        completion_pattern=completion_pattern,
                        session_name=session_name,
                        session_id=session_id,
                    )
                    state.update(
                        hybrid_pane_id=claude_hybrid_controller.pane_id,
                        hybrid_pane_pid=claude_hybrid_controller.pane_pid(),
                    )
                else:
                    result = await run_command_fn(
                        command=command,
                        cwd=agent.cwd,
                        env=agent.env,
                        timeout_seconds=looper.timeout_seconds,
                        log_path=log_path,
                        agent_kind=agent.kind,
                        patterns=patterns,
                        scan_stdout=(
                            looper.scan_stdout_for_stop_patterns
                            or agent.scan_stdout_for_stop_patterns
                        ),
                        kill_on_stop_pattern=looper.kill_on_stop_pattern,
                        completion_pattern=completion_pattern,
                        stream_output=not tail_pane_active,
                        on_process_started=record_child_process,
                    )

                if result.completion_detected:
                    loop_completion_detected = True
                loop_output_bytes += result.output_bytes

                if result.session_id:
                    session_id = result.session_id
                    if not looper.fresh_session_per_loop:
                        persistent_session_id = session_id

                state.record(
                    "prompt_completed",
                    status="running",
                    current_session_id=session_id,
                    returncode=result.returncode,
                    last_output_bytes=result.output_bytes,
                    completion_detected=result.completion_detected,
                    stop_reason=result.stop_reason,
                    retry_kind=result.retry_kind,
                    retry_after_seconds=result.retry_after_seconds,
                )

                poll_control_commands()
                if control_stop := next_control_prompt_stop():
                    return stop_for_control(control_stop)

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
                            set_tmux_window_option_fn(TMUX_STATE_OPTION, "READY")
                            set_tmux_window_option_fn(TMUX_STOP_REASON_OPTION, limit_reason[:250])
                            return stop_run(reason=limit_reason, exit_code=0)
                        if result.session_id or agent.kind == "claude":
                            attempt_first_prompt_in_session = False
                        delay_seconds = retry_delay_seconds(result, looper)
                        retry_status = retry_status_message(
                            result=result,
                            attempt=retry_count,
                            delay_seconds=delay_seconds,
                        )
                        set_tmux_window_option_fn(TMUX_STATE_OPTION, "RUN")
                        set_tmux_window_option_fn(TMUX_STOP_REASON_OPTION, retry_status[:250])
                        if should_notify_retry_wait(delay_seconds=delay_seconds, looper=looper):
                            display_tmux_message_fn(retry_notification_message(retry_status))
                        state.record(
                            "retry_wait",
                            status="retrying",
                            stop_reason=result.stop_reason,
                            retry_status=retry_status,
                            retry_count=retry_count,
                            retry_kind=result.retry_kind,
                            retry_after_seconds=delay_seconds,
                            current_session_id=session_id,
                            last_log=str(log_path),
                        )
                        print(f"\nRETRY: {result.stop_reason}")
                        print(f"last log: {log_path}")
                        print(f"sleeping {format_duration(delay_seconds)} before retry")
                        await sleep_fn(delay_seconds)
                        continue

                    print(f"\nSTOP: {result.stop_reason}")
                    print(f"last log: {log_path}")
                    set_tmux_window_option_fn(TMUX_STATE_OPTION, "READY")
                    set_tmux_window_option_fn(TMUX_STOP_REASON_OPTION, result.stop_reason[:250])
                    return stop_run(reason=result.stop_reason, exit_code=0)

                if result.returncode not in (0, None) and not looper.ignore_nonzero:
                    reason = f"command exited with code {result.returncode}"
                    print(f"\nSTOP: {reason}")
                    print(f"last log: {log_path}")
                    set_tmux_window_option_fn(TMUX_STATE_OPTION, "ERR")
                    set_tmux_window_option_fn(TMUX_STOP_REASON_OPTION, f"exit {result.returncode}")
                    return stop_run(
                        reason=reason,
                        exit_code=int(result.returncode or 1),
                        status="error",
                    )

                set_tmux_window_option_fn(TMUX_STOP_REASON_OPTION, "")
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
        state.record(
            "loop_completed",
            status="running",
            current_loop=loop_number,
            current_prompt_index=len(prompts),
            loop_duration_seconds=loop_duration_seconds,
            last_output_bytes=loop_output_bytes,
            completion_detected=loop_completion_detected,
        )

        poll_control_commands()
        if control_stop := (next_control_prompt_stop() or control_stop_after_loop):
            return stop_for_control(control_stop)

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
                        set_tmux_window_option_fn(TMUX_STATE_OPTION, "READY")
                        set_tmux_window_option_fn(TMUX_STOP_REASON_OPTION, "completion marker")
                        return stop_run(reason="completion marker", exit_code=0)
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
                    set_tmux_window_option_fn(TMUX_STATE_OPTION, "READY")
                    set_tmux_window_option_fn(TMUX_STOP_REASON_OPTION, reason[:250])
                    return stop_run(reason=reason, exit_code=0)
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
                    set_tmux_window_option_fn(TMUX_STATE_OPTION, "READY")
                    set_tmux_window_option_fn(TMUX_STOP_REASON_OPTION, reason[:250])
                    return stop_run(reason=reason, exit_code=0)
            else:
                output_decline_count = 0
            previous_loop_output_bytes = loop_output_bytes

        if output_match_pattern is not None:
            if loop_logs_match(output_match_pattern, loop_log_paths):
                output_match_count += 1
                print(
                    "output match detected "
                    f"({output_match_count}/{looper.cb_output_match_repeats}): "
                    f"{looper.cb_output_match_pattern}"
                )
                state.update(
                    output_match_detected=True,
                    output_match_count=output_match_count,
                    output_match_pattern=looper.cb_output_match_pattern,
                )
                if output_match_count >= looper.cb_output_match_repeats:
                    reason = f"output matched configured pattern for {output_match_count} loop(s)"
                    print(f"STOP: {reason}")
                    set_tmux_window_option_fn(TMUX_STATE_OPTION, "READY")
                    set_tmux_window_option_fn(TMUX_STOP_REASON_OPTION, reason[:250])
                    return stop_run(reason=reason, exit_code=0)
            else:
                if output_match_count:
                    print("output match missing; resetting output-match streak")
                output_match_count = 0
                state.update(output_match_detected=False, output_match_count=0)

        if options.dry_run:
            print("dry run complete")
            set_tmux_window_option_fn(TMUX_STATE_OPTION, "READY")
            return stop_run(reason="dry run complete", exit_code=0)

        if looper.max_loops and loop_number >= looper.max_loops:
            reason = f"max loops reached: {looper.max_loops}"
            print(reason)
            set_tmux_window_option_fn(TMUX_STATE_OPTION, "READY")
            return stop_run(reason=reason, exit_code=0)

        await sleep_fn(looper.sleep_seconds)


def run_loop_sync(*, agent: AgentConfig, looper: LooperConfig, options: RunOptions) -> int:
    return asyncio.run(run_loop(agent=agent, looper=looper, options=options))
