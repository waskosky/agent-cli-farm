from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .models import ConfigError, RunOptions, TmuxLayout


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


def argv_has_option(argv: list[str], *names: str) -> bool:
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
    layout_arg_present = argv_has_option(inner_args, "--tmux-layout")
    propagated_layout = (
        options.tmux_layout if options.tmux_layout != "auto" or layout_arg_present else "split"
    )
    env["CODEX_LOOPER_LAYOUT"] = env.get("CODEX_LOOPER_LAYOUT") or propagated_layout
    if not layout_arg_present:
        inner_args.extend(["--tmux-layout", propagated_layout])
    if not argv_has_option(inner_args, "--local"):
        inner_args.append("--local")
    if options.label and not argv_has_option(inner_args, "--label", "-l"):
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
