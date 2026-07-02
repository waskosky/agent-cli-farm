from __future__ import annotations

import json
from pathlib import Path

from .config import default_agents
from .models import DEFAULT_SEQUENCE_PROMPT_FILE, DEFAULT_SINGLE_PROMPT_FILE, LooperMode


def build_example_config(
    *,
    default_agent: str = "codex",
    mode: LooperMode = "single",
    prompt_file: str = "PROMPT.md",
    timeout_seconds: str = "7200",
    sleep_seconds: str = "2",
    max_loops: str = "0",
    max_transient_retries: str = "12",
    retry_notify_after_seconds: str = "300",
) -> str:
    return f"""# agent-looper.toml
# Default mode loops one prompt file forever. Set mode = "sequence" to split
# prompt_file on lines containing only ---.

[looper]
default_agent = {json.dumps(default_agent)}
mode = {json.dumps(mode)}
prompt_file = {json.dumps(prompt_file)}
timeout_seconds = {timeout_seconds}
sleep_seconds = {sleep_seconds}
fresh_session_per_loop = true
reload_prompt_each_loop = true
# max_loops = 0 means forever. Use --once or --max-loops for a bounded run.
max_loops = {max_loops}
# Rate-limit retries are uncapped; this caps repeated non-rate-limit transient retries.
# max_transient_retries = 0 means unlimited.
max_transient_retries = {max_transient_retries}
# Notify the tmux pane for retry waits at or above this many seconds. 0 disables.
retry_notify_after_seconds = {retry_notify_after_seconds}
log_dir = ".agent-looper/runs"
# Completion detection is opt-in. Agents should emit the marker only when done.
completion_enabled = false
completion_marker = "EXIT_SIGNAL:\\\\s*true"
completion_streak = 1
# Optional markdown checklist gate for completion.
plan_file = ""
# Optional git safety controls.
backup_enabled = false
backup_prefix = "looper-backup"
backup_keep = 10
cb_no_progress = 0
cb_output_decline = 0
cb_output_match_pattern = ""
cb_output_match_repeats = 1

[agents.claude]
kind = "claude"
# extra_args are inserted into the built-in Claude Code command templates.
# Optional sugar:
# model = "claude-opus-4-8"
# effort = "max"
# Example:
# extra_args = ["--permission-mode", "acceptEdits", "--max-turns", "20"]
# For isolated unattended sandboxes, Claude also supports:
# extra_args = ["--dangerously-skip-permissions"]
extra_args = []

[agents.codex]
kind = "codex"
# Optional sugar:
# model = "gpt-5.4"
# effort = "high"
# Example:
# extra_args = ["--sandbox", "workspace-write"]
extra_args = []

[agents.gemini]
kind = "generic"
# Gemini CLI prompt/resume flags may vary by version; override these templates
# if your installed Gemini CLI uses a different non-interactive interface.
first_command = ["gemini", "-p", "{{prompt}}"]
resume_command = ["gemini", "-p", "{{prompt}}"]
scan_stdout_for_stop_patterns = true

# For other coding agents, define command templates. Placeholders are:
# {{prompt}}, {{session}}, {{session_id}}, {{loop}}, {{prompt_index}}, {{label}}, {{run_dir}}
#
# [agents.my_agent]
# kind = "generic"
# first_command = ["my-agent", "run", "--session", "{{session}}", "{{prompt}}"]
# resume_command = ["my-agent", "run", "--resume", "{{session}}", "{{prompt}}"]
# scan_stdout_for_stop_patterns = true
"""


EXAMPLE_CONFIG = build_example_config()


EXAMPLE_PROMPTS = """Summarize the repository layout. Identify one safe cleanup, but do not modify files unless the cleanup is obvious and low-risk.
"""


def write_starter_files(
    *,
    force: bool,
    config_text: str = EXAMPLE_CONFIG,
    prompts_text: str = EXAMPLE_PROMPTS,
    prompt_path: Path = DEFAULT_SINGLE_PROMPT_FILE,
) -> list[Path]:
    files = [
        (Path("agent-looper.toml"), config_text),
        (prompt_path, prompts_text),
    ]
    written: list[Path] = []
    for path, content in files:
        if path.exists() and not force:
            print(f"exists, leaving unchanged: {path}")
            continue
        path.write_text(content, encoding="utf-8")
        written.append(path)
        print(f"wrote {path}")
    return written


def guidance_lines(
    *, initialized: bool, prompt_path: Path = DEFAULT_SINGLE_PROMPT_FILE
) -> list[str]:
    heading = (
        "Initialized Agent Looper starter files."
        if initialized
        else "Agent Looper is already initialized."
    )
    return [
        heading,
        "",
        "Next steps:",
        f"  1. Edit {prompt_path}",
        "  2. Run in the default tmux farm: codex-looper",
        "  3. Inspect activity: codex-status activity",
        "",
        "Customize defaults with: codex-looper init --interactive --force",
    ]


def print_guidance(*, initialized: bool, prompt_path: Path = DEFAULT_SINGLE_PROMPT_FILE) -> None:
    print("\n".join(guidance_lines(initialized=initialized, prompt_path=prompt_path)))


def first_run_main() -> int:
    config = Path("agent-looper.toml")
    already_initialized = config.exists() and (
        DEFAULT_SINGLE_PROMPT_FILE.exists() or DEFAULT_SEQUENCE_PROMPT_FILE.exists()
    )
    written = write_starter_files(force=False)
    print("")
    print_guidance(initialized=bool(written) and not already_initialized)
    return 0


def _ask(prompt: str, default: str) -> str:
    reply = input(f"{prompt} [{default}]: ").strip()
    return reply or default


def _ask_number(prompt: str, default: str) -> str:
    value = _ask(prompt, default)
    try:
        parsed = float(value)
    except ValueError:
        print(f"Invalid number {value!r}; using {default}.")
        return default
    if parsed <= 0:
        print(f"Number must be greater than zero; using {default}.")
        return default
    return value


def _ask_nonnegative_number(prompt: str, default: str) -> str:
    value = _ask(prompt, default)
    try:
        parsed = float(value)
    except ValueError:
        print(f"Invalid number {value!r}; using {default}.")
        return default
    if parsed < 0:
        print(f"Number must be zero or greater; using {default}.")
        return default
    return value


def _ask_nonnegative_int(prompt: str, default: str) -> str:
    value = _ask(prompt, default)
    try:
        parsed = int(value)
    except ValueError:
        print(f"Invalid integer {value!r}; using {default}.")
        return default
    if parsed < 0:
        print(f"Number must be zero or greater; using {default}.")
        return default
    return str(parsed)


def interactive_starter_content() -> tuple[str, str, Path]:
    print("Agent Looper interactive setup")
    default_agent = _ask("Default agent (codex, claude, gemini)", "codex")
    if default_agent not in default_agents():
        print(f"Unknown default agent {default_agent!r}; using codex.")
        default_agent = "codex"
    timeout_seconds = _ask_number("Timeout seconds per prompt", "7200")
    sleep_seconds = _ask_number("Sleep seconds between loops", "2")
    max_loops = _ask_nonnegative_int("Max loops (0 means forever)", "0")
    max_transient_retries = _ask_nonnegative_int(
        "Max transient retries before stopping (0 means unlimited)",
        "12",
    )
    retry_notify_after_seconds = _ask_nonnegative_number(
        "Notify for retry waits at or above seconds (0 disables)",
        "300",
    )

    print("Enter prompts one at a time. Submit a blank prompt when finished.")
    prompts: list[str] = []
    while True:
        prompt = input(f"Prompt {len(prompts) + 1}: ").strip()
        if not prompt:
            break
        prompts.append(prompt)
    if len(prompts) > 1:
        mode: LooperMode = "sequence"
        prompt_path = DEFAULT_SEQUENCE_PROMPT_FILE
        prompt_text = "\n---\n".join(prompts).strip() + "\n"
    else:
        mode = "single"
        prompt_path = DEFAULT_SINGLE_PROMPT_FILE
        prompt_text = (prompts[0].strip() + "\n") if prompts else EXAMPLE_PROMPTS
    return (
        build_example_config(
            default_agent=default_agent,
            mode=mode,
            prompt_file=str(prompt_path),
            timeout_seconds=timeout_seconds,
            sleep_seconds=sleep_seconds,
            max_loops=max_loops,
            max_transient_retries=max_transient_retries,
            retry_notify_after_seconds=retry_notify_after_seconds,
        ),
        prompt_text,
        prompt_path,
    )
