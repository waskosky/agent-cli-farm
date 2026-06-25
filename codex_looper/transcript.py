from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

LOG_LINE_PATTERN = re.compile(
    r"^\[(?P<stamp>\d{8}T\d{6}Z)\]\s+(?P<kind>[A-Za-z_]+):\s?(?P<payload>.*)$"
)


def split_logged_line(line: str) -> tuple[str | None, str]:
    text = line.rstrip("\n")
    match = LOG_LINE_PATTERN.match(text)
    if not match:
        return None, text
    return match.group("kind"), match.group("payload")


def _format_tool_use(item: dict[str, Any]) -> str | None:
    name = str(item.get("name") or item.get("tool_name") or "tool")
    tool_input = item.get("input")
    if not isinstance(tool_input, dict):
        tool_input = item.get("arguments")

    description = ""
    command = ""
    if isinstance(tool_input, dict):
        raw_description = tool_input.get("description")
        raw_command = tool_input.get("command")
        if isinstance(raw_description, str):
            description = raw_description.strip()
        if isinstance(raw_command, str):
            command = raw_command.strip()

    header = f"tool: {name}"
    if description:
        header = f"{header} - {description}"
    if command:
        return f"{header}\n$ {command}"
    if isinstance(tool_input, dict) and tool_input:
        return f"{header}\n{json.dumps(tool_input, ensure_ascii=False, default=str)}"
    if isinstance(tool_input, str) and tool_input.strip():
        return f"{header}\n{tool_input.strip()}"
    return header


def _format_tool_result(item: dict[str, Any]) -> str | None:
    content = item.get("content")
    if isinstance(content, str) and content.strip():
        prefix = "tool error" if item.get("is_error") is True else "tool result"
        return f"{prefix}:\n{content.rstrip()}"
    return None


def _format_content_item(item: Any) -> str | None:
    if isinstance(item, str):
        return item.rstrip() or None
    if not isinstance(item, dict):
        return None

    item_type = str(item.get("type", ""))
    if item_type in {"thinking", "reasoning"}:
        return None
    if item_type in {"text", "output_text", "input_text"}:
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            return text.rstrip()
    if item_type == "tool_use":
        return _format_tool_use(item)
    if item_type == "tool_result":
        return _format_tool_result(item)

    text = item.get("text") or item.get("content")
    if isinstance(text, str) and text.strip():
        return text.rstrip()
    return None


def _format_content(value: Any) -> list[str]:
    if isinstance(value, str):
        stripped = value.rstrip()
        return [stripped] if stripped else []
    if isinstance(value, list):
        rendered: list[str] = []
        for item in value:
            text = _format_content_item(item)
            if text:
                rendered.append(text)
        return rendered
    return []


def _format_codex_item(item: dict[str, Any]) -> list[str]:
    item_type = str(item.get("type", ""))
    if item_type == "message":
        return _format_content(item.get("content"))
    if item_type in {"function_call", "tool_call"}:
        rendered = _format_tool_use(item)
        return [rendered] if rendered else []
    return _format_content(item.get("content"))


def _format_agent_json_event(data: dict[str, Any]) -> str | None:
    event_type = str(data.get("type", ""))
    subtype = str(data.get("subtype", ""))
    if event_type == "system" and subtype == "thinking_tokens":
        return None
    if event_type in {"thread.started", "turn.started", "turn.completed"}:
        return None

    rendered: list[str] = []

    message = data.get("message")
    if isinstance(message, dict):
        rendered.extend(_format_content(message.get("content")))

    item = data.get("item")
    if isinstance(item, dict):
        rendered.extend(_format_codex_item(item))

    rendered.extend(_format_content(data.get("content")))

    if event_type == "rate_limit_event":
        info = data.get("rate_limit_info")
        if isinstance(info, dict):
            status = info.get("status", "unknown")
            kind = info.get("rateLimitType", "rate-limit")
            return f"rate limit event: {kind} status={status}"
        return "rate limit event"

    if event_type == "error":
        message_text = data.get("message") or data.get("error")
        if isinstance(message_text, str) and message_text.strip():
            rendered.append(f"error: {message_text.rstrip()}")

    if rendered:
        return "\n".join(text for text in rendered if text)
    return None


def format_agent_log_line(line: str) -> str | None:
    kind, payload = split_logged_line(line)
    if not payload.strip():
        return None

    stripped = payload.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            return _format_agent_json_event(data)

    if kind == "stdout" or kind is None:
        return payload.rstrip() or None
    if kind == "stderr":
        return f"stderr: {payload.rstrip()}" if payload.rstrip() else None
    return f"{kind}: {payload.rstrip()}" if payload.rstrip() else None


def transcript_log_main(argv: list[str] | None = None, *, prog: str = "transcript-log") -> int:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Render raw looper JSONL logs into a compact human-readable transcript.",
    )
    parser.parse_args(sys.argv[1:] if argv is None else argv)

    for line in sys.stdin:
        rendered = format_agent_log_line(line)
        if rendered is None:
            continue
        print(rendered)
        sys.stdout.flush()
    return 0
