from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any

try:
    import tomllib as _tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 only.
    _tomllib = None  # type: ignore[assignment]

from .models import AgentConfig, ConfigError, LoadedConfig, LooperConfig


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


def _agent_interface(value: Any, key: str, default: str) -> str:
    interface = _as_str(value, key, default, nonempty=True)
    if interface not in {"json", "hybrid"}:
        raise ConfigError(f"{key} must be json or hybrid")
    return interface


def default_agents() -> dict[str, AgentConfig]:
    return {
        "claude": AgentConfig(name="claude", kind="claude", interface="hybrid"),
        "codex": AgentConfig(name="codex", kind="codex"),
        "gemini": AgentConfig(
            name="gemini",
            kind="generic",
            first_command=["gemini", "-p", "{prompt}"],
            resume_command=["gemini", "-p", "{prompt}"],
            scan_stdout_for_stop_patterns=True,
        ),
    }


def read_config_raw(path: Path, *, tomllib_module: Any = _tomllib) -> dict[str, Any]:
    if path.exists():
        try:
            if tomllib_module is None:
                return parse_basic_toml(path.read_text(encoding="utf-8"))
            with path.open("rb") as fh:
                return tomllib_module.load(fh)
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


def load_config(
    path: Path,
    *,
    preset_paths: list[Path] | None = None,
    tomllib_module: Any = _tomllib,
) -> LoadedConfig:
    raw = read_config_raw(path, tomllib_module=tomllib_module)
    for preset_path in preset_paths or []:
        raw = merge_raw_config(raw, read_config_raw(preset_path, tomllib_module=tomllib_module))

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
    cb_output_match_pattern = _as_str(
        raw_looper.get("cb_output_match_pattern"),
        "looper.cb_output_match_pattern",
        default_looper.cb_output_match_pattern,
    )
    cb_output_match_repeats = _as_int(
        raw_looper.get("cb_output_match_repeats"),
        "looper.cb_output_match_repeats",
        default_looper.cb_output_match_repeats,
        minimum=1,
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
        reload_prompt_each_loop=_as_bool(
            raw_looper.get("reload_prompt_each_loop"),
            "looper.reload_prompt_each_loop",
            default_looper.reload_prompt_each_loop,
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
        cb_output_match_pattern=cb_output_match_pattern,
        cb_output_match_repeats=cb_output_match_repeats,
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
        interface = _agent_interface(
            value.get("interface"),
            f"agents.{name}.interface",
            base.interface if base else "json",
        )
        if interface == "hybrid" and kind != "claude":
            raise ConfigError(f"agents.{name}.interface hybrid is currently only supported for claude")
        agents[name] = AgentConfig(
            name=name,
            kind=kind,  # type: ignore[arg-type]
            interface=interface,  # type: ignore[arg-type]
            cwd=_path(value.get("cwd"), base.cwd if base else Path(".")),
            extra_args=_as_str_list(value.get("extra_args"), f"agents.{name}.extra_args")
            or (base.extra_args if base else []),
            model=_optional_str(value.get("model"), f"agents.{name}.model")
            or (base.model if base else None),
            effort=_optional_str(value.get("effort"), f"agents.{name}.effort")
            or (base.effort if base else None),
            interactive_command=(
                _as_str_list(value.get("interactive_command"), f"agents.{name}.interactive_command")
                or (base.interactive_command if base else None)
            ),
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
