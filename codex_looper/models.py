from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

VERSION = "0.3.1"
AgentKind = Literal["claude", "codex", "generic"]
AgentInterface = Literal["json", "hybrid"]
LooperMode = Literal["single", "sequence"]
TmuxLayout = Literal["auto", "single", "split"]
TMUX_STATE_OPTION = "@codex_state"
TMUX_STOP_REASON_OPTION = "@codex_stop_reason"
CURRENT_LOG_POINTER_FILENAME = "current-log.path"
DEFAULT_TIMEOUT_SECONDS = 7200.0
DEFAULT_MAX_LOOPS = 0
DEFAULT_MAX_TRANSIENT_RETRIES = 12
DEFAULT_RETRY_NOTIFY_AFTER_SECONDS = 300.0
STREAM_READ_CHUNK_BYTES = 64 * 1024
DEFAULT_SINGLE_PROMPT_FILE = Path("PROMPT.md")
DEFAULT_SEQUENCE_PROMPT_FILE = Path("prompts.md")
DEFAULT_COMPLETION_MARKER = r"EXIT_SIGNAL:\s*true"

DEFAULT_STOP_PATTERNS = [
    r"rate[\s_-]*limit(?:ed|ing)?",
    r"\b429\b",
    r"too many requests",
    r"retry[\s_-]*after",
    r"back[\s_-]*off",
    r"quota exceeded",
    r"temporarily unavailable",
    r"overloaded",
    r"timed?\s*out",
    r"deadline exceeded",
    r"request aborted",
]


class ConfigError(ValueError):
    """Raised when looper configuration is invalid."""


class PromptError(ValueError):
    """Raised when prompt loading fails."""


class CommandTemplateError(ValueError):
    """Raised when command templates cannot be rendered."""


@dataclass(frozen=True)
class AgentConfig:
    name: str
    kind: AgentKind
    interface: AgentInterface = "json"
    cwd: Path = Path(".")
    extra_args: list[str] = field(default_factory=list)
    model: str | None = None
    effort: str | None = None
    interactive_command: list[str] | None = None
    first_command: list[str] | None = None
    resume_command: list[str] | None = None
    env: dict[str, str] = field(default_factory=dict)
    scan_stdout_for_stop_patterns: bool = False


@dataclass(frozen=True)
class LooperConfig:
    default_agent: str = "codex"
    mode: LooperMode = "single"
    prompt_file: Path = DEFAULT_SINGLE_PROMPT_FILE
    mode_explicit: bool = False
    prompt_file_explicit: bool = False
    separator: str = r"^---\s*$"
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    sleep_seconds: float = 2.0
    fresh_session_per_loop: bool = True
    reload_prompt_each_loop: bool = True
    max_loops: int = DEFAULT_MAX_LOOPS
    max_transient_retries: int = DEFAULT_MAX_TRANSIENT_RETRIES
    retry_notify_after_seconds: float = DEFAULT_RETRY_NOTIFY_AFTER_SECONDS
    log_dir: Path = Path(".agent-looper/runs")
    stop_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_STOP_PATTERNS))
    kill_on_stop_pattern: bool = True
    ignore_nonzero: bool = False
    scan_stdout_for_stop_patterns: bool = False
    completion_enabled: bool = False
    completion_marker: str = DEFAULT_COMPLETION_MARKER
    completion_streak: int = 1
    plan_file: Path | None = None
    backup_enabled: bool = False
    backup_prefix: str = "looper-backup"
    backup_keep: int = 10
    cb_no_progress: int = 0
    cb_output_decline: int = 0


@dataclass(frozen=True)
class LoadedConfig:
    looper: LooperConfig
    agents: dict[str, AgentConfig]


@dataclass(frozen=True)
class RunOptions:
    agent_name: str | None
    config_path: Path
    mode: LooperMode | None = None
    prompt_file: Path | None = None
    agent_args: list[str] = field(default_factory=list)
    agent_interface: AgentInterface | None = None
    label: str | None = None
    timeout_seconds: float | None = None
    sleep_seconds: float | None = None
    max_loops: int | None = None
    max_transient_retries: int | None = None
    retry_notify_after_seconds: float | None = None
    completion_marker: str | None = None
    completion_streak: int | None = None
    plan_file: Path | None = None
    backup_enabled: bool = False
    backup_prefix: str | None = None
    backup_keep: int | None = None
    cb_no_progress: int | None = None
    cb_output_decline: int | None = None
    preset: str | None = None
    once: bool = False
    fresh_session_per_loop: bool | None = None
    cwd: Path | None = None
    dry_run: bool = False
    ignore_nonzero: bool | None = None
    hold_on_stop: bool = False
    farm_session: str | None = None
    farm_attach: bool = False
    farm_add_bin: str = "codex-add"
    tmux_layout: TmuxLayout = "auto"
    local: bool = False


@dataclass(frozen=True)
class CommandContext:
    prompt: str
    session: str
    session_id: str
    loop: int
    prompt_index: int
    label: str
    run_dir: Path

    def as_format_dict(self) -> dict[str, object]:
        return {
            "prompt": self.prompt,
            "session": self.session,
            "session_id": self.session_id,
            "loop": self.loop,
            "prompt_index": self.prompt_index,
            "label": self.label,
            "run_dir": str(self.run_dir),
        }


@dataclass
class ParsedLine:
    session_id: str | None = None
    stop_reason: str | None = None
    retry_after_seconds: float | None = None
    retry_kind: str | None = None


@dataclass
class ProcessResult:
    returncode: int | None
    session_id: str | None = None
    stop_reason: str | None = None
    retry_after_seconds: float | None = None
    retry_kind: str | None = None
    timed_out: bool = False
    completion_detected: bool = False
    output_bytes: int = 0
