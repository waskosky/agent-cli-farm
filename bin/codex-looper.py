#!/usr/bin/env python3
# ruff: noqa: E402,F401
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR_FOR_IMPORTS = Path(__file__).resolve().parent
for import_root in (SCRIPT_DIR_FOR_IMPORTS.parent, SCRIPT_DIR_FOR_IMPORTS):
    if (import_root / "codex_looper").is_dir():
        sys.path.insert(0, str(import_root))
        break

from codex_looper import cli as _cli
from codex_looper import farm as _farm
from codex_looper import runner as _runner
from codex_looper.agents import agent_extra_args, build_command, render_template
from codex_looper.cli import (
    add_run_arguments,
    control_main,
    default_agent_from_invocation,
    display_name,
    doctor_main,
    init_main,
    maybe_hold_on_stop,
    nonnegative_float,
    nonnegative_int,
    parse_run_options,
    positive_float,
    positive_int,
    resolve_agent_name,
    split_agent_args,
)
from codex_looper.config import (
    default_agents,
    merge_raw_config,
    parse_basic_toml,
    repo_root,
    resolve_preset_path,
)
from codex_looper.config import (
    load_config as _load_config_impl,
)
from codex_looper.config import (
    read_config_raw as _read_config_raw_impl,
)
from codex_looper.control import (
    OPERATOR_NOTE_DELIVERIES,
    append_operator_note,
    deliver_operator_note,
    format_operator_note_for_delivery,
    operator_notes_file_path,
    paste_text_to_tmux_pane,
    resolve_operator_note_target,
)
from codex_looper.control_panel import control_pane_main
from codex_looper.farm import (
    clean_farm_args,
    default_tmux_layout_from_env,
    resolve_self_executable,
)
from codex_looper.git_safety import (
    _run_git,
    create_backup_branch,
    git_workspace_fingerprint,
    prune_backup_branches,
)
from codex_looper.hybrid import (
    ClaudeHybridController,
    TmuxCommandResult,
    build_claude_hybrid_command,
    tmux_split_window_command,
)
from codex_looper.init import (
    EXAMPLE_CONFIG,
    EXAMPLE_PROMPTS,
    _ask,
    _ask_nonnegative_int,
    _ask_nonnegative_number,
    _ask_number,
    build_example_config,
    first_run_main,
    guidance_lines,
    interactive_starter_content,
    print_guidance,
    write_starter_files,
)
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
    AgentInterface,
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
from codex_looper.pane_status import classify_claude_output, classify_codex_output
from codex_looper.process import (
    _close_subprocess_transport,
    _safe_stream_write,
    _terminate_process_group,
)
from codex_looper.process import (
    run_command as _run_command_impl,
)
from codex_looper.prompts import (
    load_prompts,
    load_prompts_for_mode,
    resolve_prompt_defaults,
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
from codex_looper.runner import (
    apply_run_options,
    compile_completion_marker,
    compile_stop_patterns,
    make_label,
    make_run_dir,
    markdown_plan_has_unchecked_tasks,
    plan_file_all_tasks_checked,
    utc_stamp,
)
from codex_looper.tmux import (
    control_pane_command,
    current_log_pointer_path,
    display_tmux_message,
    set_tmux_window_option,
    start_tmux_log_pane,
    tmux_log_tail_command,
    transcript_renderer_command,
    update_current_log_pointer,
)
from codex_looper.transcript import format_agent_log_line, split_logged_line, transcript_log_main

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
    "EXAMPLE_CONFIG",
    "EXAMPLE_PROMPTS",
    "STREAM_READ_CHUNK_BYTES",
    "TMUX_STATE_OPTION",
    "TMUX_STOP_REASON_OPTION",
    "VERSION",
    "AgentConfig",
    "AgentInterface",
    "ClaudeHybridController",
    "CommandContext",
    "CommandTemplateError",
    "ConfigError",
    "LoadedConfig",
    "LooperConfig",
    "LooperMode",
    "OPERATOR_NOTE_DELIVERIES",
    "ParsedLine",
    "ProcessResult",
    "PromptError",
    "RunOptions",
    "TmuxLayout",
    "TmuxCommandResult",
    "_ask",
    "_ask_nonnegative_int",
    "_ask_nonnegative_number",
    "_ask_number",
    "_close_subprocess_transport",
    "_run_git",
    "_safe_stream_write",
    "_terminate_process_group",
    "add_run_arguments",
    "agent_extra_args",
    "append_operator_note",
    "apply_run_options",
    "build_command",
    "build_claude_hybrid_command",
    "build_example_config",
    "clean_farm_args",
    "classify_claude_output",
    "classify_codex_output",
    "compile_completion_marker",
    "compile_stop_patterns",
    "control_main",
    "control_pane_command",
    "control_pane_main",
    "create_backup_branch",
    "current_log_pointer_path",
    "default_agent_from_invocation",
    "default_agents",
    "default_tmux_layout_from_env",
    "deliver_operator_note",
    "display_name",
    "display_tmux_message",
    "doctor_main",
    "first_run_main",
    "format_agent_log_line",
    "format_byte_count",
    "format_duration",
    "format_loop_metrics",
    "format_operator_note_for_delivery",
    "git_workspace_fingerprint",
    "guidance_lines",
    "init_main",
    "interactive_starter_content",
    "is_retryable_stop_reason",
    "load_config",
    "load_prompts",
    "load_prompts_for_mode",
    "main",
    "make_label",
    "make_run_dir",
    "markdown_plan_has_unchecked_tasks",
    "maybe_hold_on_stop",
    "maybe_launch_farm",
    "merge_raw_config",
    "nonnegative_float",
    "nonnegative_int",
    "parse_basic_toml",
    "parse_output_line",
    "parse_run_options",
    "operator_notes_file_path",
    "paste_text_to_tmux_pane",
    "plan_file_all_tasks_checked",
    "positive_float",
    "positive_int",
    "print_guidance",
    "prune_backup_branches",
    "read_config_raw",
    "render_template",
    "repo_root",
    "resolve_agent_name",
    "resolve_operator_note_target",
    "resolve_preset_path",
    "resolve_prompt_defaults",
    "resolve_self_executable",
    "retry_delay_seconds",
    "retry_notification_message",
    "retry_status_message",
    "run_command",
    "run_command_main",
    "run_loop",
    "run_loop_sync",
    "set_tmux_window_option",
    "should_notify_retry_wait",
    "split_agent_args",
    "split_logged_line",
    "start_tmux_log_pane",
    "tmux_log_tail_command",
    "tmux_split_window_command",
    "transcript_log_main",
    "transcript_renderer_command",
    "transient_retry_limit_message",
    "transient_retry_limit_reached",
    "update_current_log_pointer",
    "write_starter_files",
]

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 only.
    tomllib = None  # type: ignore[assignment]


async def run_command(
    *,
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
    log_path: Path,
    agent_kind: str,
    patterns: list[Any],
    scan_stdout: bool,
    kill_on_stop_pattern: bool,
    completion_pattern: Any | None = None,
    stream_output: bool = True,
    on_process_started: Any | None = None,
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


async def run_loop(
    *,
    agent: AgentConfig,
    looper: LooperConfig,
    options: RunOptions,
    run_command_fn: Any | None = None,
    run_claude_hybrid_turn_fn: Any | None = None,
    set_tmux_window_option_fn: Any | None = None,
    display_tmux_message_fn: Any | None = None,
    start_tmux_log_pane_fn: Any | None = None,
    update_current_log_pointer_fn: Any | None = None,
    sleep_fn: Any | None = None,
) -> int:
    return await _runner.run_loop(
        agent=agent,
        looper=looper,
        options=options,
        run_command_fn=run_command_fn or run_command,
        run_claude_hybrid_turn_fn=run_claude_hybrid_turn_fn or _runner.run_claude_hybrid_turn,
        set_tmux_window_option_fn=set_tmux_window_option_fn or set_tmux_window_option,
        display_tmux_message_fn=display_tmux_message_fn or display_tmux_message,
        start_tmux_log_pane_fn=start_tmux_log_pane_fn or start_tmux_log_pane,
        update_current_log_pointer_fn=update_current_log_pointer_fn or update_current_log_pointer,
        sleep_fn=sleep_fn or asyncio.sleep,
    )


def run_loop_sync(*, agent: AgentConfig, looper: LooperConfig, options: RunOptions) -> int:
    return asyncio.run(run_loop(agent=agent, looper=looper, options=options))


def read_config_raw(path: Path) -> dict[str, Any]:
    return _read_config_raw_impl(path, tomllib_module=tomllib)


def load_config(path: Path, *, preset_paths: list[Path] | None = None) -> LoadedConfig:
    return _load_config_impl(path, preset_paths=preset_paths, tomllib_module=tomllib)


def maybe_launch_farm(options: RunOptions, original_argv: list[str]) -> int | None:
    return _farm.maybe_launch_farm(options, original_argv)


def run_command_main(
    argv: list[str] | None = None,
    *,
    default_agent: str | None = None,
    load_config_fn: Any | None = None,
    run_loop_sync_fn: Any | None = None,
    maybe_launch_farm_fn: Any | None = None,
) -> int:
    return _cli.run_command_main(
        argv,
        default_agent=default_agent,
        load_config_fn=load_config_fn or load_config,
        run_loop_sync_fn=run_loop_sync_fn or run_loop_sync,
        maybe_launch_farm_fn=maybe_launch_farm_fn or maybe_launch_farm,
    )


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
        subparsers.add_parser("control", help="queue control commands for a running looper")
        subparsers.add_parser("control-pane", help="run the tmux looper control pane")
        subparsers.add_parser("transcript-log", help="render raw looper JSONL logs")
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
    if command == "control":
        return control_main(rest)
    if command == "control-pane":
        return control_pane_main(rest, prog=f"{display_name()} control-pane")
    if command == "transcript-log":
        return transcript_log_main(rest, prog=f"{display_name()} transcript-log")

    return run_command_main(real_argv, default_agent=default_agent)


if __name__ == "__main__":
    raise SystemExit(main())
