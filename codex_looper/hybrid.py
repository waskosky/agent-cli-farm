from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TextIO

from .agents import agent_extra_args
from .models import AgentConfig, ConfigError, ProcessResult
from .pane_status import classify_claude_output, classify_codex_output, strip_ansi
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
HYBRID_INHERITED_ENV_KEYS = ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_HOME")
CLAUDE_NONTERMINAL_STOP_REASONS = frozenset({"tool_use", "pause_turn"})


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
    stop_reason: str | None = None


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


@dataclass(frozen=True)
class CodexSessionEvent:
    event_type: str
    payload_type: str | None
    session_id: str | None
    turn_id: str | None
    role: str | None
    is_user_event: bool
    is_assistant_event: bool


@dataclass(frozen=True)
class CodexSessionTail:
    offset: int
    raw_text: str
    events: tuple[CodexSessionEvent, ...]


@dataclass(frozen=True)
class CodexHybridAssessment:
    pane_state: str
    session_id: str | None
    next_offset: int
    event_count: int
    last_event_type: str | None
    last_payload_type: str | None
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


def extract_session_id_from_codex_session(path: Path, *, max_lines: int = 50) -> str | None:
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
                payload = data.get("payload")
                if isinstance(payload, dict):
                    for key in ("session_id", "id"):
                        value = payload.get(key)
                        if isinstance(value, str) and value:
                            return value
    except OSError:
        return None
    return None


def _codex_session_metadata(path: Path, *, max_lines: int = 20) -> tuple[float | None, Path | None]:
    """Read the session start time and cwd without trusting the file mtime."""
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
                if data.get("type") != "session_meta":
                    continue
                payload = data.get("payload")
                if not isinstance(payload, dict):
                    return None, None

                raw_started_at = payload.get("timestamp") or data.get("timestamp")
                started_at = None
                if isinstance(raw_started_at, int | float):
                    started_at = float(raw_started_at)
                elif isinstance(raw_started_at, str) and raw_started_at:
                    try:
                        parsed = datetime.fromisoformat(raw_started_at.replace("Z", "+00:00"))
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                        started_at = parsed.timestamp()
                    except ValueError:
                        pass

                raw_cwd = payload.get("cwd")
                session_cwd = Path(raw_cwd).expanduser() if isinstance(raw_cwd, str) else None
                return started_at, session_cwd
    except OSError:
        return None, None
    return None, None


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
    raw_stop_reason = (
        message.get("stop_reason") if isinstance(message, dict) else data.get("stop_reason")
    )

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
        stop_reason=raw_stop_reason if isinstance(raw_stop_reason, str) else None,
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


def parse_codex_session_line(line: str) -> CodexSessionEvent | None:
    if not line.strip():
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    payload = data.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    event_type = data.get("type")
    payload_type = payload.get("type")
    role = payload.get("role")
    turn_id = payload.get("turn_id")
    session_id = None
    if event_type == "session_meta":
        for key in ("session_id", "id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                session_id = value
                break

    normalized_payload_type = payload_type if isinstance(payload_type, str) else None
    normalized_role = role if isinstance(role, str) else None
    is_user_event = normalized_payload_type == "user_message" or (
        normalized_payload_type == "message" and normalized_role == "user"
    )
    is_assistant_event = normalized_payload_type == "agent_message" or (
        normalized_payload_type == "message" and normalized_role == "assistant"
    )
    return CodexSessionEvent(
        event_type=event_type if isinstance(event_type, str) else "",
        payload_type=normalized_payload_type,
        session_id=session_id,
        turn_id=turn_id if isinstance(turn_id, str) else None,
        role=normalized_role,
        is_user_event=is_user_event,
        is_assistant_event=is_assistant_event,
    )


def read_new_codex_session_events(path: Path, *, offset: int = 0) -> CodexSessionTail:
    try:
        with path.open("rb") as handle:
            handle.seek(max(offset, 0))
            data = handle.read()
            next_offset = handle.tell()
    except OSError:
        return CodexSessionTail(offset=offset, raw_text="", events=())

    raw_text = data.decode("utf-8", errors="replace")
    events = tuple(
        event
        for line in raw_text.splitlines()
        if (event := parse_codex_session_line(line)) is not None
    )
    return CodexSessionTail(offset=next_offset, raw_text=raw_text, events=events)


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
    assistant_events = [
        event
        for event in tail.events
        if event.role == "assistant" or event.event_type == "assistant"
    ]
    last_assistant_stop_reason = assistant_events[-1].stop_reason if assistant_events else None
    terminal_assistant_event_seen = bool(
        last_assistant_stop_reason
        and last_assistant_stop_reason not in CLAUDE_NONTERMINAL_STOP_REASONS
    )
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
        if terminal_assistant_event_seen:
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
                confidence="high",
                reason="terminal assistant event overrides stale pane activity",
            )
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
        if last_assistant_stop_reason == "tool_use":
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
                reason="assistant requested tool use; waiting for terminal assistant event",
            )
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


def assess_codex_hybrid_signals(
    pane_state: str,
    *,
    session_path: Path | None,
    previous_offset: int = 0,
) -> CodexHybridAssessment:
    tail = (
        read_new_codex_session_events(session_path, offset=previous_offset)
        if session_path is not None
        else CodexSessionTail(offset=previous_offset, raw_text="", events=())
    )
    session_id = extract_session_id_from_codex_session(session_path) if session_path else None
    for event in tail.events:
        if event.session_id:
            session_id = event.session_id
            break

    last_event_type = tail.events[-1].event_type if tail.events else None
    last_payload_type = tail.events[-1].payload_type if tail.events else None
    role_events = [event.role for event in tail.events if event.role]
    last_role = role_events[-1] if role_events else None
    assistant_event_seen = any(event.is_assistant_event for event in tail.events)
    user_event_seen = any(event.is_user_event for event in tail.events)

    normalized_state = pane_state.upper()
    if normalized_state == "ERR":
        return CodexHybridAssessment(
            pane_state=normalized_state,
            session_id=session_id,
            next_offset=tail.offset,
            event_count=len(tail.events),
            last_event_type=last_event_type,
            last_payload_type=last_payload_type,
            last_role=last_role,
            assistant_event_seen=assistant_event_seen,
            user_event_seen=user_event_seen,
            ready_to_send_next=False,
            confidence="error",
            reason="pane state is ERR",
        )
    if normalized_state == "RUN":
        return CodexHybridAssessment(
            pane_state=normalized_state,
            session_id=session_id,
            next_offset=tail.offset,
            event_count=len(tail.events),
            last_event_type=last_event_type,
            last_payload_type=last_payload_type,
            last_role=last_role,
            assistant_event_seen=assistant_event_seen,
            user_event_seen=user_event_seen,
            ready_to_send_next=False,
            confidence="running",
            reason="pane state is RUN",
        )

    if assistant_event_seen:
        confidence: HybridConfidence = "high"
        reason = "pane ready and assistant event seen in Codex session file"
    elif tail.events:
        confidence = "medium"
        reason = "pane ready and Codex session file advanced"
    elif session_id:
        confidence = "medium"
        reason = "pane ready and Codex session id identified"
    else:
        confidence = "low"
        reason = "pane ready only"

    return CodexHybridAssessment(
        pane_state=normalized_state,
        session_id=session_id,
        next_offset=tail.offset,
        event_count=len(tail.events),
        last_event_type=last_event_type,
        last_payload_type=last_payload_type,
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
    inherited_env: Mapping[str, str] | None = None,
) -> list[str]:
    inherited_source = os.environ if inherited_env is None else inherited_env
    pane_env = {
        key: value for key in HYBRID_INHERITED_ENV_KEYS if (value := inherited_source.get(key))
    }
    pane_env.update(env)

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
    for key, value in sorted(pane_env.items()):
        command.extend(["-e", f"{key}={value}"])
    command.extend(["-c", str(cwd), shlex.join(pane_command)])
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


def build_codex_hybrid_command(agent: AgentConfig) -> list[str]:
    if agent.interactive_command:
        return list(agent.interactive_command)
    return ["codex", "--no-alt-screen", *agent_extra_args(agent)]


def _write_log_lines(log_file: TextIO, raw_text: str) -> None:
    for line in raw_text.splitlines():
        log_file.write(f"[{utc_stamp()}] stdout: {line}\n")
    log_file.flush()


def _claude_trust_prompt_detected(output: str) -> bool:
    clean = strip_ansi(output).lower()
    return "trust" in clean and ("folder" in clean or "files" in clean)


def _codex_trust_prompt_detected(output: str) -> bool:
    clean = strip_ansi(output).lower()
    return "trust" in clean and "directory" in clean and "press enter to continue" in clean


def _codex_update_prompt_detected(output: str) -> bool:
    clean = strip_ansi(output).lower()
    return all(
        marker in clean
        for marker in ("update available", "update now", "skip", "press enter to continue")
    )


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
            raise ConfigError(
                "Claude hybrid interface requires tmux; use --interface json outside tmux"
            )
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
        self.run_tmux(
            ["tmux", "set-window-option", "-q", "-t", self.pane_id, "remain-on-exit", "on"]
        )
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
        result = self.run_tmux(["tmux", "display-message", "-p", "-t", self.pane_id, "#{pane_pid}"])
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

    def _scan_open_session_path(self) -> Path | None:
        root_pid = self.pane_pid()
        if not root_pid:
            return None
        for pid in self.descendant_pids(root_pid):
            result = self.run_tmux(["lsof", "-Fn", "-p", pid])
            if result.returncode not in (0, 1):
                continue
            for line in result.stdout.splitlines():
                if not line.startswith("n"):
                    continue
                path = line[1:]
                if "/.claude/projects/" in path and path.endswith(".jsonl"):
                    return Path(path)
        return None

    def recent_session_path(self, *, since: float) -> Path | None:
        slack = 2.0
        open_path = self._scan_open_session_path()
        if open_path is not None:
            try:
                if open_path.stat().st_mtime >= since - slack:
                    return open_path
            except OSError:
                pass
        claude_root = Path.home() / ".claude" / "projects"
        if not claude_root.exists():
            return None
        candidates: list[Path] = []
        for path in claude_root.glob("*/*.jsonl"):
            try:
                if path.stat().st_mtime >= since - slack:
                    candidates.append(path)
            except OSError:
                continue
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def discover_session_path(self) -> Path | None:
        if self.session_path and self.session_path.exists():
            return self.session_path
        if open_path := self._scan_open_session_path():
            self.session_path = open_path
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
                detail = (
                    result.stderr or result.stdout or "unknown tmux prompt paste failure"
                ).strip()
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

        prompt_sent_at = time.time()
        self.send_prompt(prompt)
        deadline = time.monotonic() + timeout_seconds
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_file:
            while time.monotonic() < deadline:
                pane_output = self.capture_output()
                pane_state = classify_claude_output(pane_output)
                session_path = self.discover_session_path()
                recent_session_path = self.recent_session_path(since=prompt_sent_at)
                if recent_session_path is not None and recent_session_path != session_path:
                    session_path = recent_session_path
                    self.session_path = recent_session_path
                    prompt_offset = 0
                    logged_offset = 0
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
                session_id=extract_session_id_from_claude_session(session_path)
                if session_path
                else None,
                stop_reason=stop_reason,
                retry_after_seconds=retry_after_seconds,
                retry_kind=retry_kind,
                completion_detected=completion_detected,
                output_bytes=output_bytes,
            )
        return ProcessResult(
            returncode=0,
            session_id=extract_session_id_from_claude_session(session_path)
            if session_path
            else None,
            stop_reason=f"Claude hybrid timeout after {timeout_seconds:g} seconds",
            timed_out=True,
            completion_detected=completion_detected,
            output_bytes=output_bytes,
        )


@dataclass
class CodexHybridController:
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
            raise ConfigError(
                "Codex hybrid interface requires tmux; use --interface json outside tmux"
            )
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
            raise ConfigError(f"failed to start Codex hybrid pane: {detail}")
        self.pane_id = result.stdout.strip().splitlines()[-1]
        self.run_tmux(
            ["tmux", "set-window-option", "-q", "-t", self.pane_id, "remain-on-exit", "on"]
        )
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
        update_prompt_snapshot: str | None = None
        while time.monotonic() < deadline:
            output = self.capture_output()
            if _codex_trust_prompt_detected(output):
                raise ConfigError(
                    "Codex hybrid pane is waiting for workspace trust; answer it manually, "
                    "pre-trust the workspace, or pass --dangerously-bypass-hook-trust, then rerun"
                )
            if _codex_update_prompt_detected(output) and update_prompt_snapshot is None:
                update_prompt_snapshot = strip_ansi(output)
                result = self.run_tmux(
                    ["tmux", "send-keys", "-t", self.pane_id or "", "2", "Enter"]
                )
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout or "unknown tmux failure").strip()
                    raise ConfigError(f"failed to skip Codex update prompt: {detail}")
                self.sleep_fn(1.0)
                continue
            if update_prompt_snapshot is not None and strip_ansi(output) == update_prompt_snapshot:
                self.sleep_fn(1.0)
                continue
            if output.strip() and classify_codex_output(output) == "READY":
                return
            self.sleep_fn(1.0)
        raise ConfigError("Codex hybrid pane did not become ready before timeout")

    def pane_pid(self) -> str | None:
        if not self.pane_id:
            return None
        result = self.run_tmux(["tmux", "display-message", "-p", "-t", self.pane_id, "#{pane_pid}"])
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

    def codex_sessions_root(self) -> Path:
        raw_home = self.env.get("CODEX_HOME") or os.environ.get("CODEX_HOME")
        if not raw_home:
            return Path.home() / ".codex" / "sessions"
        codex_home = Path(raw_home).expanduser()
        if not codex_home.is_absolute():
            codex_home = self.cwd / codex_home
        return codex_home / "sessions"

    def discover_session_path(self) -> Path | None:
        if self.session_path and self.session_path.exists():
            return self.session_path
        root_pid = self.pane_pid()
        sessions_root = self.codex_sessions_root().resolve()
        if root_pid:
            for pid in self.descendant_pids(root_pid):
                result = self.run_tmux(["lsof", "-Fn", "-p", pid])
                if result.returncode not in (0, 1):
                    continue
                for line in result.stdout.splitlines():
                    if not line.startswith("n"):
                        continue
                    path = Path(line[1:]).resolve()
                    if path.suffix == ".jsonl" and sessions_root in path.parents:
                        self.session_path = path
                        return self.session_path
        if not sessions_root.exists():
            return None
        metadata_candidates: list[tuple[float, Path]] = []
        unknown_candidates: list[Path] = []
        expected_cwd = self.cwd.resolve()
        for path in sessions_root.glob("**/*.jsonl"):
            try:
                if path.stat().st_mtime < self.started_at - 2:
                    continue
            except OSError:
                continue
            session_started_at, session_cwd = _codex_session_metadata(path)
            if session_started_at is None:
                unknown_candidates.append(path)
                continue
            if session_started_at < self.started_at - 2:
                continue
            if session_cwd is not None and session_cwd.resolve() != expected_cwd:
                continue
            metadata_candidates.append((session_started_at, path))

        if metadata_candidates:
            self.session_path = max(metadata_candidates, key=lambda item: item[0])[1]
        elif unknown_candidates:
            self.session_path = max(unknown_candidates, key=lambda path: path.stat().st_mtime)
        else:
            return None
        return self.session_path

    def send_prompt(self, prompt: str) -> None:
        if not self.pane_id:
            raise ConfigError("Codex hybrid pane has not been started")
        for command in tmux_prompt_paste_commands(self.pane_id, buffer_name=self.buffer_name):
            input_text = prompt if command[1:2] == ["load-buffer"] else None
            result = self.run_tmux(command, input_text=input_text)
            if result.returncode != 0:
                detail = (
                    result.stderr or result.stdout or "unknown tmux prompt paste failure"
                ).strip()
                raise ConfigError(f"failed to send prompt to Codex hybrid pane: {detail}")
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
                pane_state = classify_codex_output(pane_output)
                session_path = self.discover_session_path()
                if session_path is None:
                    self.sleep_fn(1.0)
                    continue

                new_tail = read_new_codex_session_events(session_path, offset=logged_offset)
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
                            agent_kind="codex",
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

                assessment = assess_codex_hybrid_signals(
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
                session_id=extract_session_id_from_codex_session(session_path)
                if session_path
                else None,
                stop_reason=stop_reason,
                retry_after_seconds=retry_after_seconds,
                retry_kind=retry_kind,
                completion_detected=completion_detected,
                output_bytes=output_bytes,
            )
        return ProcessResult(
            returncode=0,
            session_id=extract_session_id_from_codex_session(session_path)
            if session_path
            else None,
            stop_reason=f"Codex hybrid timeout after {timeout_seconds:g} seconds",
            timed_out=True,
            completion_detected=completion_detected,
            output_bytes=output_bytes,
        )


HybridController = ClaudeHybridController | CodexHybridController


def reset_hybrid_controller(controller: HybridController) -> str | None:
    """End the current interactive process so the next turn starts a new session."""
    previous_pane_id = controller.pane_id
    if previous_pane_id:
        result = controller.run_tmux(["tmux", "kill-pane", "-t", previous_pane_id])
        detail = (result.stderr or result.stdout or "").strip()
        pane_missing = any(
            marker in detail.lower()
            for marker in ("can't find pane", "no such pane", "can't find window")
        )
        if result.returncode != 0 and not pane_missing:
            raise ConfigError(
                f"failed to reset hybrid pane {previous_pane_id}: "
                f"{detail or f'exit {result.returncode}'}"
            )
    controller.pane_id = None
    controller.session_path = None
    controller.started_at = time.time()
    return previous_pane_id
