from __future__ import annotations

import importlib.util
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType

from .models import (
    CURRENT_LOG_POINTER_FILENAME,
    DEFAULT_COMPLETION_MARKER,
    DEFAULT_MAX_LOOPS,
    DEFAULT_MAX_TRANSIENT_RETRIES,
    DEFAULT_RETRY_NOTIFY_AFTER_SECONDS,
    DEFAULT_SEQUENCE_PROMPT_FILE,
    DEFAULT_SINGLE_PROMPT_FILE,
    DEFAULT_STOP_PATTERNS,
    DEFAULT_TIMEOUT_SECONDS,
    STREAM_READ_CHUNK_BYTES,
    TMUX_STATE_OPTION,
    TMUX_STOP_REASON_OPTION,
    VERSION,
    AgentConfig,
    AgentKind,
    CommandContext,
    CommandTemplateError,
    ConfigError,
    LoadedConfig,
    LooperConfig,
    LooperMode,
    ParsedLine,
    ProcessResult,
    PromptError,
    RunOptions,
    TmuxLayout,
)
from .git_safety import create_backup_branch, git_workspace_fingerprint, prune_backup_branches
from .prompts import load_prompts, load_prompts_for_mode, resolve_prompt_defaults
from .process import run_command
from .retry import (
    format_byte_count,
    format_duration,
    format_loop_metrics,
    is_retryable_stop_reason,
    parse_output_line,
    retry_delay_seconds,
    retry_notification_message,
    retry_status_message,
    should_notify_retry_wait,
    transient_retry_limit_message,
    transient_retry_limit_reached,
)
from .tmux import (
    current_log_pointer_path,
    display_tmux_message,
    set_tmux_window_option,
    start_tmux_log_pane,
    tmux_log_tail_command,
    update_current_log_pointer,
)


@lru_cache(maxsize=1)
def _cli_module() -> ModuleType:
    repo_root = Path(__file__).resolve().parent.parent
    looper_path = repo_root / "bin" / "codex-looper.py"
    spec = importlib.util.spec_from_file_location("_codex_looper_cli", looper_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load looper CLI module from {looper_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_config(*args: object, **kwargs: object) -> object:
    return _cli_module().load_config(*args, **kwargs)


def run_command_main(*args: object, **kwargs: object) -> int:
    return _cli_module().run_command_main(*args, **kwargs)


__all__ = [
    "CURRENT_LOG_POINTER_FILENAME",
    "DEFAULT_COMPLETION_MARKER",
    "DEFAULT_MAX_LOOPS",
    "DEFAULT_MAX_TRANSIENT_RETRIES",
    "DEFAULT_RETRY_NOTIFY_AFTER_SECONDS",
    "DEFAULT_SEQUENCE_PROMPT_FILE",
    "DEFAULT_SINGLE_PROMPT_FILE",
    "DEFAULT_STOP_PATTERNS",
    "DEFAULT_TIMEOUT_SECONDS",
    "STREAM_READ_CHUNK_BYTES",
    "TMUX_STATE_OPTION",
    "TMUX_STOP_REASON_OPTION",
    "VERSION",
    "AgentConfig",
    "AgentKind",
    "CommandContext",
    "CommandTemplateError",
    "ConfigError",
    "create_backup_branch",
    "current_log_pointer_path",
    "display_tmux_message",
    "LoadedConfig",
    "LooperConfig",
    "LooperMode",
    "ParsedLine",
    "ProcessResult",
    "PromptError",
    "RunOptions",
    "TmuxLayout",
    "load_config",
    "load_prompts",
    "load_prompts_for_mode",
    "format_byte_count",
    "format_duration",
    "format_loop_metrics",
    "git_workspace_fingerprint",
    "is_retryable_stop_reason",
    "parse_output_line",
    "prune_backup_branches",
    "retry_delay_seconds",
    "retry_notification_message",
    "retry_status_message",
    "resolve_prompt_defaults",
    "run_command",
    "run_command_main",
    "set_tmux_window_option",
    "should_notify_retry_wait",
    "start_tmux_log_pane",
    "tmux_log_tail_command",
    "transient_retry_limit_message",
    "transient_retry_limit_reached",
    "update_current_log_pointer",
]
