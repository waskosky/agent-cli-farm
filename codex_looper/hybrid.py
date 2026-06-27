from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


UUID_PATTERN = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
HybridConfidence = Literal["high", "medium", "low", "running", "error"]


@dataclass(frozen=True)
class ClaudeSessionEvent:
    event_type: str
    session_id: str | None
    uuid: str | None
    parent_uuid: str | None
    role: str | None
    timestamp: str | None
    content_types: tuple[str, ...]


@dataclass(frozen=True)
class ClaudeSessionTail:
    offset: int
    raw_text: str
    events: tuple[ClaudeSessionEvent, ...]


@dataclass(frozen=True)
class ClaudeHybridAssessment:
    pane_state: str
    session_id: str | None
    next_offset: int
    event_count: int
    last_event_type: str | None
    last_role: str | None
    assistant_event_seen: bool
    user_event_seen: bool
    ready_to_send_next: bool
    confidence: HybridConfidence
    reason: str


def extract_uuid_from_session_path(path: Path) -> str | None:
    match = UUID_PATTERN.search(path.with_suffix("").name)
    return match.group(1) if match else None


def extract_session_id_from_claude_session(path: Path, *, max_lines: int = 50) -> str | None:
    session_id = extract_uuid_from_session_path(path)
    if session_id:
        return session_id
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= max_lines:
                    break
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                value = data.get("sessionId")
                if isinstance(value, str) and value:
                    return value
    except OSError:
        return None
    return None


def _content_types_from_value(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return ("text",) if value else ()
    if not isinstance(value, list):
        return ()
    content_types: list[str] = []
    for item in value:
        if isinstance(item, dict):
            item_type = item.get("type")
            if isinstance(item_type, str) and item_type:
                content_types.append(item_type)
    return tuple(content_types)


def parse_claude_session_line(line: str) -> ClaudeSessionEvent | None:
    if not line.strip():
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    message = data.get("message")
    role = None
    content: Any = data.get("content")
    if isinstance(message, dict):
        raw_role = message.get("role")
        if isinstance(raw_role, str) and raw_role:
            role = raw_role
        content = message.get("content")

    event_type = data.get("type")
    session_id = data.get("sessionId")
    uuid = data.get("uuid")
    parent_uuid = data.get("parentUuid")
    timestamp = data.get("timestamp")
    return ClaudeSessionEvent(
        event_type=event_type if isinstance(event_type, str) else "",
        session_id=session_id if isinstance(session_id, str) else None,
        uuid=uuid if isinstance(uuid, str) else None,
        parent_uuid=parent_uuid if isinstance(parent_uuid, str) else None,
        role=role,
        timestamp=timestamp if isinstance(timestamp, str) else None,
        content_types=_content_types_from_value(content),
    )


def read_new_claude_session_events(path: Path, *, offset: int = 0) -> ClaudeSessionTail:
    try:
        with path.open("rb") as handle:
            handle.seek(max(offset, 0))
            data = handle.read()
            next_offset = handle.tell()
    except OSError:
        return ClaudeSessionTail(offset=offset, raw_text="", events=())

    raw_text = data.decode("utf-8", errors="replace")
    events = tuple(
        event
        for line in raw_text.splitlines()
        if (event := parse_claude_session_line(line)) is not None
    )
    return ClaudeSessionTail(offset=next_offset, raw_text=raw_text, events=events)


def assess_claude_hybrid_signals(
    pane_state: str,
    *,
    session_path: Path | None,
    previous_offset: int = 0,
) -> ClaudeHybridAssessment:
    tail = (
        read_new_claude_session_events(session_path, offset=previous_offset)
        if session_path is not None
        else ClaudeSessionTail(offset=previous_offset, raw_text="", events=())
    )
    session_id = extract_session_id_from_claude_session(session_path) if session_path else None
    for event in tail.events:
        if event.session_id:
            session_id = event.session_id
            break

    last_event_type = tail.events[-1].event_type if tail.events else None
    role_events = [event.role for event in tail.events if event.role]
    last_role = role_events[-1] if role_events else None
    assistant_event_seen = any(
        event.role == "assistant" or event.event_type == "assistant" for event in tail.events
    )
    user_event_seen = any(event.role == "user" or event.event_type == "user" for event in tail.events)

    normalized_state = pane_state.upper()
    if normalized_state == "ERR":
        return ClaudeHybridAssessment(
            pane_state=normalized_state,
            session_id=session_id,
            next_offset=tail.offset,
            event_count=len(tail.events),
            last_event_type=last_event_type,
            last_role=last_role,
            assistant_event_seen=assistant_event_seen,
            user_event_seen=user_event_seen,
            ready_to_send_next=False,
            confidence="error",
            reason="pane state is ERR",
        )
    if normalized_state == "RUN":
        return ClaudeHybridAssessment(
            pane_state=normalized_state,
            session_id=session_id,
            next_offset=tail.offset,
            event_count=len(tail.events),
            last_event_type=last_event_type,
            last_role=last_role,
            assistant_event_seen=assistant_event_seen,
            user_event_seen=user_event_seen,
            ready_to_send_next=False,
            confidence="running",
            reason="pane state is RUN",
        )

    if assistant_event_seen:
        confidence: HybridConfidence = "high"
        reason = "pane ready and assistant event seen in Claude session file"
    elif tail.events:
        confidence = "medium"
        reason = "pane ready and Claude session file advanced"
    elif session_id:
        confidence = "medium"
        reason = "pane ready and Claude session id identified"
    else:
        confidence = "low"
        reason = "pane ready only"

    return ClaudeHybridAssessment(
        pane_state=normalized_state,
        session_id=session_id,
        next_offset=tail.offset,
        event_count=len(tail.events),
        last_event_type=last_event_type,
        last_role=last_role,
        assistant_event_seen=assistant_event_seen,
        user_event_seen=user_event_seen,
        ready_to_send_next=True,
        confidence=confidence,
        reason=reason,
    )


def tmux_prompt_paste_commands(
    pane_id: str,
    *,
    buffer_name: str = "codex-looper-prompt",
) -> list[list[str]]:
    return [
        ["tmux", "load-buffer", "-b", buffer_name, "-"],
        ["tmux", "paste-buffer", "-d", "-b", buffer_name, "-t", pane_id],
        ["tmux", "send-keys", "-t", pane_id, "Enter"],
    ]
