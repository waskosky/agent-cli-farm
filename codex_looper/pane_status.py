from __future__ import annotations

import re

ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"
CODEX_IDLE_PROMPT_PATTERN = r"(?:\u276f|\u203a|codex>)"
CODEX_IDLE_PROMPT_AT_END_PATTERN = rf"(?:^\s*{CODEX_IDLE_PROMPT_PATTERN}\s*$)\s*\Z"
CODEX_WAITING_PROMPT_PATTERN = r"^(?:Approve|Allow)\b.*\b(?:y/n|yes/no|yes|no)\b"
CODEX_ERROR_PATTERN = r"^(?:Error:|ERROR:|Traceback \(most recent call last\):|panic:)"
CODEX_PROCESSING_PATTERN = r"\b(thinking|working|running|executing|processing|analyzing)\b"
CODEX_USER_PREFIX_PATTERN = r"^You\b"
CODEX_ASSISTANT_PREFIX_PATTERN = r"^(?:assistant|codex|agent)\s*:"

CLAUDE_IDLE_PROMPT_PATTERN = r">(?:\s|\u00a0)"
CLAUDE_IDLE_PROMPT_AT_END_PATTERN = rf"(?:^\s*{CLAUDE_IDLE_PROMPT_PATTERN}\s*$)\s*\Z"
CLAUDE_WAITING_PROMPT_PATTERN = r"\u276f.*\d+\."
CLAUDE_PROCESSING_PATTERN = (
    r"[\u2736\u2722\u273d\u273b\u00b7\u2733].*\u2026.*\(esc to interrupt.*\)"
    r"|(?:^|\n)\s*[\u2736\u2722\u273d\u273b\u00b7\u2733]?\s*"
    r"(?i:vibing|envisioning|thinking|processing|reading|writing|searching|running|working)"
    r"\u2026"
    r"|(?:^|\n)\s*[\u2736\u2722\u273d\u273b\u00b7\u2733]\s+\S[^\n]*\u2026[^\n]*"
    r"(?:tokens|thinking|effort|\d+s|\d+m)"
)


def strip_ansi(text: str) -> str:
    return re.sub(ANSI_CODE_PATTERN, "", text)


def tail_lines(text: str, lines: int = 50) -> str:
    return "\n".join(text.splitlines()[-lines:])


def non_empty_tail_lines(text: str, lines: int = 8) -> str:
    recent = [line for line in text.splitlines() if line.strip()]
    return "\n".join(recent[-lines:])


def is_codex_command(cmd: str) -> bool:
    return bool(re.search(r"\bcodex\b", cmd, re.IGNORECASE))


def is_claude_command(cmd: str) -> bool:
    return bool(re.search(r"\bclaude\b", cmd, re.IGNORECASE))


def classify_codex_output(output: str | None) -> str:
    if not output:
        return "ERR"
    clean = strip_ansi(output)
    tail_output = tail_lines(clean)
    last_user = None
    for match in re.finditer(CODEX_USER_PREFIX_PATTERN, clean, re.IGNORECASE | re.MULTILINE):
        last_user = match

    output_after_last_user = clean[last_user.start() :] if last_user else clean
    assistant_after_last_user = bool(
        last_user
        and re.search(
            CODEX_ASSISTANT_PREFIX_PATTERN,
            output_after_last_user,
            re.IGNORECASE | re.MULTILINE,
        )
    )

    if last_user is not None:
        if not assistant_after_last_user:
            if re.search(
                CODEX_WAITING_PROMPT_PATTERN,
                output_after_last_user,
                re.IGNORECASE | re.MULTILINE,
            ):
                return "READY"
            if re.search(
                CODEX_ERROR_PATTERN,
                output_after_last_user,
                re.IGNORECASE | re.MULTILINE,
            ):
                return "ERR"
    else:
        if re.search(CODEX_WAITING_PROMPT_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
            return "READY"
        if re.search(CODEX_ERROR_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
            return "ERR"

    if re.search(CODEX_IDLE_PROMPT_AT_END_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
        return "READY"

    if re.search(CODEX_PROCESSING_PATTERN, non_empty_tail_lines(clean), re.IGNORECASE):
        return "RUN"
    return "READY"


def classify_claude_output(output: str | None) -> str:
    if not output:
        return "ERR"
    clean = strip_ansi(output)
    tail_output = tail_lines(clean)
    if re.search(CLAUDE_WAITING_PROMPT_PATTERN, tail_output, re.MULTILINE):
        return "READY"
    if re.search(CLAUDE_PROCESSING_PATTERN, tail_output, re.MULTILINE):
        return "RUN"
    if re.search(CLAUDE_IDLE_PROMPT_AT_END_PATTERN, tail_output, re.MULTILINE):
        return "READY"
    return "READY"


def aggregate_window_state(states: list[str]) -> str:
    if not states:
        return "ERR"
    if "RUN" in states:
        return "RUN"
    if "ERR" in states:
        return "ERR"
    return "READY"
