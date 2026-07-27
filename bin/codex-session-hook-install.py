#!/usr/bin/env python3
"""Install the Codex Farm session-identity hook without replacing user hooks."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

HOOK_BASENAME = "codex-session-hook.py"
SESSION_START_MATCHER = "startup|resume|clear|compact"


class ConfigError(ValueError):
    pass


def default_hooks_file() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    return Path(codex_home) / "hooks.json" if codex_home else Path.home() / ".codex" / "hooks.json"


def load_config(path: Path, *, missing_ok: bool) -> dict[str, Any] | None:
    if path.is_symlink():
        raise ConfigError(f"Refusing symlinked hooks config: {path}")
    if not path.exists():
        return {} if missing_ok else None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Malformed hooks config {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"Malformed hooks config {path}: top level must be an object")
    hooks = value.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise ConfigError(f"Malformed hooks config {path}: hooks must be an object")
    return value


def command_targets_farm_hook(command: object) -> bool:
    if not isinstance(command, str):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    return any(Path(token).name == HOOK_BASENAME for token in tokens)


def remove_farm_handlers(hooks: dict[str, Any]) -> None:
    for event in ("SessionStart", "UserPromptSubmit"):
        groups = hooks.get(event)
        if groups is None:
            continue
        if not isinstance(groups, list):
            raise ConfigError(f"Malformed hooks config: {event} must be an array")
        retained_groups: list[Any] = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ConfigError(
                    f"Malformed hooks config: {event} matcher groups need a hooks array"
                )
            retained_handlers = [
                handler
                for handler in group["hooks"]
                if not (
                    isinstance(handler, dict)
                    and handler.get("type") == "command"
                    and command_targets_farm_hook(handler.get("command"))
                )
            ]
            if retained_handlers:
                retained_group = dict(group)
                retained_group["hooks"] = retained_handlers
                retained_groups.append(retained_group)
        if retained_groups:
            hooks[event] = retained_groups
        else:
            hooks.pop(event, None)


def farm_handler(command: str) -> dict[str, Any]:
    return {"type": "command", "command": command, "timeout": 3}


def hook_command(hook_path: Path, python_command: str | None) -> str:
    command = [str(hook_path)]
    if python_command:
        command.insert(0, python_command)
    return shlex.join(command)


def merge_config(
    config: dict[str, Any],
    hook_path: Path,
    python_command: str | None,
) -> dict[str, Any]:
    updated = dict(config)
    hooks = dict(updated.get("hooks", {}))
    remove_farm_handlers(hooks)
    command = hook_command(hook_path, python_command)
    hooks.setdefault("SessionStart", []).append(
        {
            "matcher": SESSION_START_MATCHER,
            "hooks": [farm_handler(command)],
        }
    )
    hooks.setdefault("UserPromptSubmit", []).append({"hooks": [farm_handler(command)]})
    updated["hooks"] = hooks
    return updated


def handler_is_current(handler: object, command: str) -> bool:
    return (
        isinstance(handler, dict)
        and handler.get("type") == "command"
        and handler.get("command") == command
        and handler.get("timeout") == 3
    )


def config_is_current(
    config: dict[str, Any],
    hook_path: Path,
    python_command: str | None,
) -> bool:
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        return False
    command = hook_command(hook_path, python_command)
    expected = {
        "SessionStart": SESSION_START_MATCHER,
        "UserPromptSubmit": None,
    }
    for event, matcher in expected.items():
        groups = hooks.get(event)
        if not isinstance(groups, list):
            return False
        matches = 0
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                continue
            if matcher is not None and group.get("matcher") != matcher:
                continue
            if matcher is None and "matcher" in group:
                continue
            matches += sum(handler_is_current(handler, command) for handler in group["hooks"])
        if matches != 1:
            return False
    return True


def serialized_config(config: dict[str, Any]) -> bytes:
    return (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def write_config(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ConfigError(f"Refusing symlinked hooks config: {path}")
    if path.exists():
        try:
            if path.read_bytes() == content:
                path.chmod(0o600)
                return
        except OSError as exc:
            raise ConfigError(f"Unable to read hooks config {path}: {exc}") from exc

    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install or verify the Codex Farm session-identity hook."
    )
    parser.add_argument("--hooks-file", type=Path, default=default_hooks_file())
    parser.add_argument(
        "--hook-command",
        type=Path,
        default=Path(__file__).resolve().with_name(HOOK_BASENAME),
    )
    parser.add_argument(
        "--python-command",
        help="supported Python executable to place before the hook path",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit successfully only when both current handlers are installed",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = load_config(args.hooks_file, missing_ok=not args.check)
        if args.check:
            return (
                0
                if config is not None
                and config_is_current(
                    config,
                    args.hook_command,
                    args.python_command,
                )
                else 1
            )
        assert config is not None
        updated = merge_config(
            config,
            args.hook_command,
            args.python_command,
        )
        write_config(args.hooks_file, serialized_config(updated))
    except (ConfigError, OSError) as exc:
        print(f"codex-session-hook-install: {exc}", file=sys.stderr)
        return 2
    print(f"Installed Codex session hook in {args.hooks_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
