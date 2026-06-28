from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TextIO

from .agents import agent_extra_args
from .models import AgentConfig, ConfigError, ProcessResult
from .pane_status import classify_claude_output, strip_ansi
from .retry import parse_output_line

UUID_PATTERN = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
HybridConfidence = Literal["high", "medium", "low", "running", "error"]
CommandRunner = Callable[..., "TmuxCommandResult"]
SleepFn = Callable[[float], None]
DEFAULT_HYBRID_SPLIT_SIZE = "70%"
DEFAULT_HYBRID_CAPTURE_LINES = 220
DEFAULT_HYBRID_READY_TIMEOUT_SECONDS = 60.0
DEFAULT_HYBRID_PASTE_SUBMIT_DELAY_SECONDS = 0.25
HYBRID_PASTE_SUBMIT_BYTES_PER_SECOND = 50_000
MAX_HYBRID_PASTE_SUBMIT_DELAY_SECONDS = 2.0


@dataclass(frozen=True)
class TmuxCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


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
    user_event_seen = any(
        event.role == "user" or event.event_type == "user" for event in tail.events
    )

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


def prompt_submit_delay_seconds(prompt: str) -> float:
    prompt_bytes = len(prompt.encode("utf-8"))
    size_delay = prompt_bytes / HYBRID_PASTE_SUBMIT_BYTES_PER_SECOND
    return min(
        MAX_HYBRID_PASTE_SUBMIT_DELAY_SECONDS,
        DEFAULT_HYBRID_PASTE_SUBMIT_DELAY_SECONDS + size_delay,
    )


def tmux_split_window_command(
    pane_command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    split_size: str = DEFAULT_HYBRID_SPLIT_SIZE,
) -> list[str]:
    command = [
        "tmux",
        "split-window",
        "-P",
        "-F",
        "#{pane_id}",
        "-d",
        "-v",
        "-l",
        split_size,
    ]
    shell_command = (
        ["env", *(f"{key}={value}" for key, value in sorted(env.items())), *pane_command]
        if env
        else pane_command
    )
    command.extend(["-c", str(cwd), shlex.join(shell_command)])
    return command


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def default_command_runner(
    command: list[str],
    *,
    input_text: str | None = None,
) -> TmuxCommandResult:
    try:
        result = subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        return TmuxCommandResult(returncode=127, stderr=str(exc))
    return TmuxCommandResult(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def build_claude_hybrid_command(agent: AgentConfig) -> list[str]:
    if agent.interactive_command:
        return list(agent.interactive_command)
    return ["claude", *agent_extra_args(agent)]


def _write_log_lines(log_file: TextIO, raw_text: str) -> None:
    for line in raw_text.splitlines():
        log_file.write(f"[{utc_stamp()}] stdout: {line}\n")
    log_file.flush()


def _claude_trust_prompt_detected(output: str) -> bool:
    clean = strip_ansi(output).lower()
    return "trust" in clean and ("folder" in clean or "files" in clean)


@dataclass
class ClaudeHybridController:
    command: list[str]
    cwd: Path
    env: dict[str, str]
    command_runner: CommandRunner = default_command_runner
    sleep_fn: SleepFn = time.sleep
    require_tmux: bool = True
    pane_id: str | None = None
    buffer_name: str = field(default_factory=lambda: f"codex-looper-prompt-{os.getpid()}")
    split_size: str = DEFAULT_HYBRID_SPLIT_SIZE
    capture_lines: int = DEFAULT_HYBRID_CAPTURE_LINES
    ready_timeout_seconds: float = DEFAULT_HYBRID_READY_TIMEOUT_SECONDS
    started_at: float = field(default_factory=time.time)
    session_path: Path | None = None

    def run_tmux(self, command: list[str], *, input_text: str | None = None) -> TmuxCommandResult:
        return self.command_runner(command, input_text=input_text)

    def ensure_started(self, *, timeout_seconds: float) -> None:
        if self.pane_id:
            return
        if self.require_tmux and not os.environ.get("TMUX"):
            raise ConfigError("Claude hybrid interface requires tmux; use --interface json outside tmux")
        result = self.run_tmux(
            tmux_split_window_command(
                self.command,
                cwd=self.cwd,
                env=self.env,
                split_size=self.split_size,
            )
        )
        if result.returncode != 0 or not result.stdout.strip():
            detail = (result.stderr or result.stdout or "unknown tmux split-window failure").strip()
            raise ConfigError(f"failed to start Claude hybrid pane: {detail}")
        self.pane_id = result.stdout.strip().splitlines()[-1]
        self.run_tmux(["tmux", "set-window-option", "-q", "-t", self.pane_id, "remain-on-exit", "on"])
        self.wait_until_ready(timeout_seconds=min(self.ready_timeout_seconds, timeout_seconds))

    def capture_output(self) -> str:
        if not self.pane_id:
            return ""
        result = self.run_tmux(
            ["tmux", "capture-pane", "-t", self.pane_id, "-e", "-p", "-S", f"-{self.capture_lines}"]
        )
        return result.stdout if result.returncode == 0 else ""

    def wait_until_ready(self, *, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            output = self.capture_output()
            if _claude_trust_prompt_detected(output):
                raise ConfigError(
                    "Claude hybrid pane is waiting for workspace trust; answer it manually "
                    "or pre-trust the workspace, then rerun"
                )
            if output.strip() and classify_claude_output(output) == "READY":
                return
            self.sleep_fn(1.0)
        raise ConfigError("Claude hybrid pane did not become ready before timeout")

    def pane_pid(self) -> str | None:
        if not self.pane_id:
            return None
        result = self.run_tmux(["tmux", "list-panes", "-t", self.pane_id, "-F", "#{pane_pid}"])
        if result.returncode != 0:
            return None
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return lines[0] if lines else None

    def descendant_pids(self, root_pid: str) -> list[str]:
        seen: list[str] = []
        queue = [root_pid]
        while queue:
            pid = queue.pop(0)
            if pid in seen:
                continue
            seen.append(pid)
            result = self.run_tmux(["pgrep", "-P", pid])
            if result.returncode not in (0, 1):
                continue
            queue.extend(line.strip() for line in result.stdout.splitlines() if line.strip())
        return seen

    def discover_session_path(self) -> Path | None:
        if self.session_path and self.session_path.exists():
            return self.session_path
        root_pid = self.pane_pid()
        if root_pid:
            for pid in self.descendant_pids(root_pid):
                result = self.run_tmux(["lsof", "-Fn", "-p", pid])
                if result.returncode not in (0, 1):
                    continue
                for line in result.stdout.splitlines():
                    if not line.startswith("n"):
                        continue
                    path = line[1:]
                    if "/.claude/projects/" in path and path.endswith(".jsonl"):
                        self.session_path = Path(path)
                        return self.session_path
        claude_root = Path.home() / ".claude" / "projects"
        if not claude_root.exists():
            return None
        candidates = [
            path
            for path in claude_root.glob("*/*.jsonl")
            if path.stat().st_mtime >= self.started_at - 2
        ]
        if not candidates:
            return None
        self.session_path = max(candidates, key=lambda path: path.stat().st_mtime)
        return self.session_path

    def send_prompt(self, prompt: str) -> None:
        if not self.pane_id:
            raise ConfigError("Claude hybrid pane has not been started")
        for command in tmux_prompt_paste_commands(self.pane_id, buffer_name=self.buffer_name):
            input_text = prompt if command[1:2] == ["load-buffer"] else None
            result = self.run_tmux(command, input_text=input_text)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "unknown tmux prompt paste failure").strip()
                raise ConfigError(f"failed to send prompt to Claude hybrid pane: {detail}")
            if command[1:2] == ["paste-buffer"]:
                self.sleep_fn(prompt_submit_delay_seconds(prompt))

    def run_turn(
        self,
        *,
        prompt: str,
        timeout_seconds: float,
        log_path: Path,
        completion_pattern: re.Pattern[str] | None,
        stop_patterns: list[re.Pattern[str]],
    ) -> ProcessResult:
        self.ensure_started(timeout_seconds=timeout_seconds)
        session_path = self.discover_session_path()
        prompt_offset = session_path.stat().st_size if session_path and session_path.exists() else 0
        logged_offset = prompt_offset
        output_bytes = 0
        completion_detected = False
        stop_reason = None
        retry_after_seconds = None
        retry_kind = None

        self.send_prompt(prompt)
        deadline = time.monotonic() + timeout_seconds
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_file:
            while time.monotonic() < deadline:
                pane_output = self.capture_output()
                pane_state = classify_claude_output(pane_output)
                session_path = self.discover_session_path()
                if session_path is None:
                    self.sleep_fn(1.0)
                    continue

                new_tail = read_new_claude_session_events(session_path, offset=logged_offset)
                if new_tail.raw_text:
                    output_bytes += len(new_tail.raw_text.encode("utf-8"))
                    _write_log_lines(log_file, new_tail.raw_text)
                    logged_offset = new_tail.offset
                    if completion_pattern and completion_pattern.search(new_tail.raw_text):
                        completion_detected = True
                    for line in new_tail.raw_text.splitlines():
                        parsed = parse_output_line(
                            line=line,
                            stream="stdout",
                            agent_kind="claude",
                            patterns=stop_patterns,
                            scan_stdout=True,
                        )
                        if parsed.stop_reason:
                            stop_reason = parsed.stop_reason
                            retry_after_seconds = parsed.retry_after_seconds
                            retry_kind = parsed.retry_kind
                            break
                if completion_pattern and completion_pattern.search(pane_output):
                    completion_detected = True
                if stop_reason:
                    break

                assessment = assess_claude_hybrid_signals(
                    pane_state,
                    session_path=session_path,
                    previous_offset=prompt_offset,
                )
                if (
                    assessment.ready_to_send_next
                    and assessment.user_event_seen
                    and assessment.assistant_event_seen
                ):
                    return ProcessResult(
                        returncode=0,
                        session_id=assessment.session_id,
                        stop_reason=None,
                        completion_detected=completion_detected,
                        output_bytes=output_bytes,
                    )
                self.sleep_fn(1.0)

        if stop_reason:
            return ProcessResult(
                returncode=0,
                session_id=extract_session_id_from_claude_session(session_path) if session_path else None,
                stop_reason=stop_reason,
                retry_after_seconds=retry_after_seconds,
                retry_kind=retry_kind,
                completion_detected=completion_detected,
                output_bytes=output_bytes,
            )
        return ProcessResult(
            returncode=0,
            session_id=extract_session_id_from_claude_session(session_path) if session_path else None,
            stop_reason=f"Claude hybrid timeout after {timeout_seconds:g} seconds",
            timed_out=True,
            completion_detected=completion_detected,
            output_bytes=output_bytes,
        )
