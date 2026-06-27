from __future__ import annotations

import json
import math
import re
import time
from typing import Any

from .models import LooperConfig, ParsedLine, ProcessResult

RETRYABLE_STOP_REASON_PATTERN = re.compile(
    r"rate[\s_-]*limit|"
    r"\b429\b|"
    r"too many requests|"
    r"retry[\s_-]*after|"
    r"back[\s_-]*off|"
    r"quota exceeded|"
    r"temporarily unavailable|"
    r"overloaded",
    re.IGNORECASE,
)
RATE_LIMIT_STOP_REASON_PATTERN = re.compile(
    r"rate[\s_-]*limit|"
    r"\b429\b|"
    r"too many requests|"
    r"quota exceeded",
    re.IGNORECASE,
)
RELATIVE_RETRY_DELAY_KEYS = (
    "retry_after_seconds",
    "retryAfterSeconds",
    "retry_after",
    "retryAfter",
    "reset_after_seconds",
    "resetAfterSeconds",
    "reset_after",
    "resetAfter",
)
ABSOLUTE_RETRY_RESET_KEYS = ("resetsAt", "reset_at", "resetAt")
INFORMATIONAL_SYSTEM_SUBTYPES = {
    "task_notification",
    "task_started",
    "task_updated",
    "thinking_tokens",
}


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


def is_retryable_stop_reason(reason: str) -> bool:
    return bool(RETRYABLE_STOP_REASON_PATTERN.search(reason))


def classify_retry_kind(reason: str) -> str | None:
    if not is_retryable_stop_reason(reason):
        return None
    if RATE_LIMIT_STOP_REASON_PATTERN.search(reason):
        return "rate_limit"
    return "transient"


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        out = float(value)
        return out if math.isfinite(out) else None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            out = float(stripped)
        except ValueError:
            return None
        return out if math.isfinite(out) else None
    return None


def _extract_retry_after_seconds(data: dict[str, Any], *, now_epoch: float) -> float | None:
    sources: list[dict[str, Any]] = [data]
    info = data.get("rate_limit_info")
    if isinstance(info, dict):
        sources.insert(0, info)

    for source in sources:
        for key in RELATIVE_RETRY_DELAY_KEYS:
            delay = _coerce_float(source.get(key))
            if delay is not None:
                return max(0.0, delay)

    for source in sources:
        for key in ABSOLUTE_RETRY_RESET_KEYS:
            reset_epoch = _coerce_float(source.get(key))
            if reset_epoch is not None:
                return max(0.0, reset_epoch - now_epoch)

    return None


def retry_delay_seconds(result: ProcessResult, looper: LooperConfig) -> float:
    if result.retry_after_seconds is not None:
        return max(0.0, result.retry_after_seconds)
    return looper.sleep_seconds


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:g}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:g}m"
    hours = minutes / 60
    return f"{hours:g}h"


def format_byte_count(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    kib = size / 1024
    if kib < 1024:
        return f"{kib:g}KiB"
    mib = kib / 1024
    return f"{mib:g}MiB"


def format_loop_metrics(*, loop_number: int, duration_seconds: float, output_bytes: int) -> str:
    return (
        f"loop metrics: loop={loop_number} "
        f"duration={format_duration(duration_seconds)} "
        f"output={format_byte_count(output_bytes)}"
    )


def retry_status_message(*, result: ProcessResult, attempt: int, delay_seconds: float) -> str:
    kind = result.retry_kind or classify_retry_kind(result.stop_reason or "") or "retryable"
    reason = result.stop_reason or "retryable provider signal"
    return f"retrying {kind} attempt {attempt}; next in {format_duration(delay_seconds)}: {reason}"


def should_notify_retry_wait(*, delay_seconds: float, looper: LooperConfig) -> bool:
    return (
        looper.retry_notify_after_seconds > 0 and delay_seconds >= looper.retry_notify_after_seconds
    )


def retry_notification_message(retry_status: str) -> str:
    return f"Looper retry wait: {retry_status}"


def transient_retry_limit_message(*, result: ProcessResult, max_retries: int) -> str:
    reason = result.stop_reason or "retryable provider signal"
    return f"transient retry limit reached after {max_retries} attempts: {reason}"


def transient_retry_limit_reached(
    *, result: ProcessResult, retry_count: int, looper: LooperConfig
) -> bool:
    if looper.max_transient_retries <= 0:
        return False
    retry_kind = result.retry_kind or classify_retry_kind(result.stop_reason or "")
    return retry_kind != "rate_limit" and retry_count > looper.max_transient_retries


def parse_output_line(
    *,
    line: str,
    stream: str,
    agent_kind: str,
    patterns: list[re.Pattern[str]],
    scan_stdout: bool,
    now_epoch: float | None = None,
) -> ParsedLine:
    stripped = line.strip()
    parsed = ParsedLine()
    now_epoch = time.time() if now_epoch is None else now_epoch

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
            if status in {"allowed", "allowed_warning"}:
                return parsed
            if status == "rejected" or status is None:
                parsed.stop_reason = f"rate limit event: status={status or 'unknown'}"
                parsed.retry_after_seconds = _extract_retry_after_seconds(data, now_epoch=now_epoch)
                parsed.retry_kind = "rate_limit"
                return parsed

        if (
            event_type == "result"
            and subtype == "success"
            and data.get("is_error") is not True
            and data.get("api_error_status") in {None, ""}
        ):
            return parsed

        if event_type == "system" and subtype in INFORMATIONAL_SYSTEM_SUBTYPES:
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
                parsed.retry_after_seconds = _extract_retry_after_seconds(data, now_epoch=now_epoch)
                parsed.retry_kind = classify_retry_kind(match)
                return parsed

        return parsed

    if stream == "stderr" or scan_stdout:
        match = _match_text(line, patterns)
        if match:
            parsed.stop_reason = f"stop pattern detected in {stream}: {match!r}"
            parsed.retry_kind = classify_retry_kind(match)

    return parsed
