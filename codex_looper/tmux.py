from __future__ import annotations

import os
import secrets
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .models import CURRENT_LOG_POINTER_FILENAME, RunOptions


def set_tmux_window_option(name: str, value: str) -> None:
    if not os.environ.get("TMUX"):
        return
    tmux = shutil.which("tmux")
    if not tmux:
        return
    subprocess.run([tmux, "set-window-option", "-q", name, value], check=False)


def display_tmux_message(message: str) -> None:
    if not os.environ.get("TMUX"):
        return
    tmux = shutil.which("tmux")
    if not tmux:
        return
    subprocess.run([tmux, "display-message", message], check=False)


def current_log_pointer_path(run_dir: Path) -> Path:
    return run_dir / CURRENT_LOG_POINTER_FILENAME


def transcript_renderer_command() -> str:
    return shlex.join([sys.executable, "-u", str(Path(sys.argv[0]).resolve()), "transcript-log"])


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def tmux_log_tail_command(*, pointer_path: Path, supervisor_pid: int) -> str:
    pointer = shlex.quote(str(pointer_path))
    supervisor = shlex.quote(str(supervisor_pid))
    renderer = transcript_renderer_command()
    return (
        "export CODEX_LOOPER_TAIL_PANE=1; "
        f"pointer={pointer}; supervisor={supervisor}; "
        'printf "Waiting for looper log pointer: %s\\n" "$pointer"; '
        "last=''; tail_pid=''; "
        'while kill -0 "$supervisor" 2>/dev/null; do '
        'next=$(cat "$pointer" 2>/dev/null || true); '
        'if [ -n "$next" ] && [ "$next" != "$last" ]; then '
        'if [ -n "$tail_pid" ]; then '
        'kill "$tail_pid" 2>/dev/null || true; '
        'wait "$tail_pid" 2>/dev/null || true; '
        "fi; "
        'printf "\\n==> %s <==\\n" "$next"; '
        f'tail -n +1 -F "$next" | {renderer} & tail_pid=$!; last="$next"; '
        "fi; "
        "sleep 1; "
        "done; "
        'if [ -n "$tail_pid" ]; then '
        'kill "$tail_pid" 2>/dev/null || true; '
        'wait "$tail_pid" 2>/dev/null || true; '
        "fi; "
        'printf "\\nLooper supervisor exited; log tail stopped.\\n"'
    )


def start_tmux_log_pane(run_dir: Path, options: RunOptions) -> bool:
    if options.tmux_layout != "split":
        return False
    if not os.environ.get("TMUX"):
        return False
    tmux = shutil.which("tmux")
    if not tmux:
        return False

    pointer_path = current_log_pointer_path(run_dir)
    _atomic_write_text(pointer_path, "")
    set_tmux_window_option("remain-on-exit", "on")
    result = subprocess.run(
        [
            tmux,
            "split-window",
            "-d",
            "-v",
            "-l",
            "35%",
            tmux_log_tail_command(pointer_path=pointer_path, supervisor_pid=os.getpid()),
        ],
        check=False,
    )
    return result.returncode == 0


def update_current_log_pointer(*, run_dir: Path, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)
    _atomic_write_text(current_log_pointer_path(run_dir), f"{log_path}\n")
