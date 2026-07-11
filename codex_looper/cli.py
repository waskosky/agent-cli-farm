from __future__ import annotations

import argparse
import math
import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from .config import load_config, resolve_preset_path
from .control import (
    ControlError,
    append_control_command,
    append_focus_update,
    deliver_operator_note,
    force_stop_from_state,
    format_stop_signal_results,
    interrupt_from_state,
    select_control_run,
)
from .control_panel import control_pane_main
from .farm import default_tmux_layout_from_env, maybe_launch_farm
from .init import (
    EXAMPLE_CONFIG,
    EXAMPLE_PROMPTS,
    first_run_main,
    interactive_starter_content,
    print_guidance,
    write_starter_files,
)
from .models import (
    DEFAULT_SEQUENCE_PROMPT_FILE,
    DEFAULT_SINGLE_PROMPT_FILE,
    VERSION,
    AgentConfig,
    CommandTemplateError,
    ConfigError,
    LoadedConfig,
    LooperConfig,
    PromptError,
    RunOptions,
)
from .runner import apply_run_options, make_label, run_loop_sync
from .status_state import repair_stale_state_file

LoadConfigFn = Callable[..., LoadedConfig]
RunLoopSyncFn = Callable[..., int]
MaybeLaunchFarmFn = Callable[[RunOptions, list[str]], int | None]


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
        "--interface",
        choices=("json", "hybrid"),
        help=(
            "agent interface override; Codex and Claude default to hybrid, "
            "json keeps the non-interactive JSON stream path"
        ),
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
    parser.add_argument(
        "--cb-output-match",
        dest="cb_output_match_pattern",
        help="stop after completed loop output matches this regex for the configured repeat count",
    )
    parser.add_argument(
        "--cb-output-match-repeats",
        type=positive_int,
        help="matching loops required for --cb-output-match before stopping",
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
        agent_interface=args.interface,
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
        cb_output_match_pattern=args.cb_output_match_pattern,
        cb_output_match_repeats=args.cb_output_match_repeats,
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


def run_command_main(
    argv: list[str] | None = None,
    *,
    default_agent: str | None = None,
    load_config_fn: LoadConfigFn = load_config,
    run_loop_sync_fn: RunLoopSyncFn = run_loop_sync,
    maybe_launch_farm_fn: MaybeLaunchFarmFn = maybe_launch_farm,
) -> int:
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
        farm_result = maybe_launch_farm_fn(options, real_argv)
        if farm_result is not None:
            return farm_result

        preset_paths = [resolve_preset_path(options.preset)] if options.preset else []
        loaded = load_config_fn(options.config_path, preset_paths=preset_paths)
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
        agent: AgentConfig = loaded.agents[agent_name]
        if options.agent_interface is not None:
            agent = replace(agent, interface=options.agent_interface)
        result = run_loop_sync_fn(agent=agent, looper=looper, options=options)
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


def _add_control_target_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("label", nargs="?", help="looper label or run id")
    parser.add_argument(
        "--state-root",
        default=os.environ.get("CODEX_LOOPER_STATE_ROOT", ".agent-looper/runs"),
        help="directory containing looper run directories",
    )
    parser.add_argument("--run-dir", help="exact looper run directory")
    parser.add_argument(
        "--include-stopped",
        action="store_true",
        help="allow selecting stopped runs when no active run matches",
    )


def _operator_note_text(args: argparse.Namespace) -> str:
    note = getattr(args, "note", None)
    note_file = getattr(args, "note_file", None)
    if note and note_file:
        raise ControlError("--note cannot be combined with --note-file")
    if note_file:
        try:
            return Path(note_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise ControlError(f"failed to read note file {note_file}: {exc}") from exc
    if note:
        return str(note)
    raise ControlError("operator note text is required; use --note or --note-file")


def _focus_summary_text(args: argparse.Namespace) -> str:
    summary = getattr(args, "summary", None)
    summary_file = getattr(args, "summary_file", None)
    if summary and summary_file:
        raise ControlError("--summary cannot be combined with --summary-file")
    if summary_file:
        try:
            return Path(summary_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise ControlError(f"failed to read summary file {summary_file}: {exc}") from exc
    if summary:
        return str(summary)
    raise ControlError("focus summary is required; use --summary or --summary-file")


def control_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=f"{display_name()} control",
        description="Queue file-backed control commands for a running looper.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    stop_parser = subparsers.add_parser("stop", help="request a looper stop")
    _add_control_target_arguments(stop_parser)
    timing = stop_parser.add_mutually_exclusive_group()
    timing.add_argument("--after-prompt", action="store_true", help="stop after the current prompt")
    timing.add_argument("--after-loop", action="store_true", help="stop after the current loop")
    timing.add_argument(
        "--now", action="store_true", help="queue interrupt_now and send SIGINT if possible"
    )
    stop_parser.add_argument(
        "--force",
        action="store_true",
        help="imply --now, then escalate SIGINT to SIGTERM and SIGKILL for known looper targets",
    )
    stop_parser.add_argument(
        "--grace-seconds",
        type=nonnegative_float,
        default=5.0,
        help="seconds to wait between forced stop escalation stages",
    )
    stop_parser.add_argument("--reason", default="operator requested stop", help="operator note")

    note_parser = subparsers.add_parser("note", help="record or send an operator note")
    _add_control_target_arguments(note_parser)
    note_parser.add_argument("--note", help="operator note text")
    note_parser.add_argument("--note-file", help="file containing operator note text")
    note_parser.add_argument(
        "--delivery",
        choices=("record", "btw", "prompt"),
        default="btw",
        help="record only, paste through Claude /btw, or paste as a normal prompt",
    )
    note_parser.add_argument("--actor", default="operator", help="actor name to record")
    note_parser.add_argument("--pane", help="explicit tmux pane id target")
    note_parser.add_argument("--tmux-session", help="restrict tmux pane fallback to one session")
    note_parser.add_argument(
        "--allow-pane-scan",
        action="store_true",
        help="allow tmux pane scanning when the selected run has no hybrid pane id",
    )

    focus_parser = subparsers.add_parser(
        "focus",
        help="record the current high-level looper focus",
    )
    _add_control_target_arguments(focus_parser)
    focus_parser.add_argument("--summary", help="one-sentence high-level focus summary")
    focus_parser.add_argument("--summary-file", help="file containing focus summary text")
    focus_parser.add_argument("--actor", default="agent", help="actor name to record")
    focus_parser.add_argument(
        "--source",
        default="manual",
        help="short source label, for example prompt, cli, or control-pane",
    )

    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.command not in {"stop", "note", "focus"}:
        parser.error(f"unsupported control command: {args.command}")
    if args.command == "stop" and args.force and (args.after_prompt or args.after_loop):
        parser.error("--force cannot be combined with --after-prompt or --after-loop")

    action = "stop_after_loop"
    if args.command == "stop" and args.after_prompt:
        action = "stop_after_prompt"
    elif args.command == "stop" and (args.now or args.force):
        action = "interrupt_now"

    try:
        state = select_control_run(
            state_root=Path(args.state_root),
            label=args.label,
            run_dir=Path(args.run_dir) if args.run_dir else None,
            include_stopped=args.include_stopped,
        )
        run_dir = Path(str(state["run_dir"]))
        if args.command == "note":
            note_result = deliver_operator_note(
                run_dir=run_dir,
                state=state,
                note=_operator_note_text(args),
                delivery=args.delivery,
                actor=args.actor,
                pane_id=args.pane or "",
                tmux_session=args.tmux_session or "",
                allow_pane_scan=args.allow_pane_scan,
            )
        elif args.command == "focus":
            focus_record = append_focus_update(
                run_dir,
                summary=_focus_summary_text(args),
                actor=args.actor,
                source=args.source,
            )
        else:
            record = append_control_command(
                run_dir,
                action=action,
                reason=args.reason,
            )
    except ControlError as exc:
        print(f"looper control error: {exc}", file=sys.stderr)
        return 2

    target = state.get("label") or state.get("run_id") or run_dir.name
    if args.command == "note":
        status = str(note_result.get("status") or "unknown")
        note_id = str(note_result.get("note", {}).get("id") or "")
        print(f"operator note {status} for {target} at {run_dir}")
        if note_id:
            print(f"note id: {note_id}")
        if note_result.get("target", {}).get("paneId"):
            print(f"pane: {note_result['target']['paneId']}")
        if note_result.get("error"):
            print(f"delivery error: {note_result['error']}", file=sys.stderr)
            return 2
        return 0
    if args.command == "focus":
        print(f"focus updated for {target} at {run_dir}")
        print(f"focus id: {focus_record['id']}")
        print(f"summary: {focus_record['summary']}")
        return 0

    print(f"queued {record['action']} for {target} at {run_dir}")
    if args.force:
        results = force_stop_from_state(state, grace_seconds=args.grace_seconds)
        detail = format_stop_signal_results(results)
        if detail:
            print(detail)
        state_path = run_dir / "state.json"
        try:
            _, repaired, stale_reason = repair_stale_state_file(state_path)
        except Exception as exc:
            print(f"stale-state repair failed: {exc}", file=sys.stderr)
        else:
            if repaired and stale_reason:
                print(f"repaired stale looper state: {stale_reason}")
    elif args.now:
        detail = interrupt_from_state(state)
        if detail:
            print(detail)
    return 0


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

    return run_command_main(real_argv, default_agent=default_agent)
