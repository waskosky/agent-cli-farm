#!/usr/bin/env bash
set -euo pipefail

launcher_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
plugin_root="$(cd -- "$launcher_dir/.." && pwd)"
viewer_tty_state=""
viewer_tty_muted=0

restore_viewer_tty() {
    if [[ "$viewer_tty_muted" -eq 1 && -n "$viewer_tty_state" ]]; then
        stty "$viewer_tty_state" 2>/dev/null || true
        viewer_tty_muted=0
    fi
}

# The plugin assembles a temporary transcript before starting less. On a busy
# host, that can leave an apparently empty popup for a few seconds. Terminal
# echo is still enabled during that startup window, so navigation keys such as
# Page Up are otherwise rendered as literal escape sequences. Make the
# read-only nature of the popup clear immediately and suppress that echo until
# the pager takes control of the terminal.
if [[ "${1:-}" == "view" && -t 0 && -t 1 ]]; then
    viewer_tty_state="$(stty -g 2>/dev/null || true)"
    if [[ -n "$viewer_tty_state" ]] && stty -echo 2>/dev/null; then
        viewer_tty_muted=1
        trap restore_viewer_tty EXIT
    fi
    printf '\033[2J\033[H%s\r\n%s\r\n' \
        'Loading deep history (read-only)...' \
        'Page Up/Page Down scroll once loaded; q closes.'
fi

configured_python=""
if [[ -r "$launcher_dir/.codexfarm-python" ]]; then
    IFS= read -r configured_python < "$launcher_dir/.codexfarm-python" || configured_python=""
fi

resolve_executable() {
    local candidate="$1"
    case "$candidate" in
        */*) [[ -x "$candidate" ]] && printf '%s\n' "$candidate" ;;
        *) command -v "$candidate" 2>/dev/null ;;
    esac
}

resolved=""
for candidate in \
    "${CODEXFARM_DEEP_HISTORY_PYTHON_BIN:-}" \
    "${TMUX_DEEP_HISTORY_PYTHON:-}" \
    "${CODEXFARM_PYTHON_BIN:-}" \
    "$configured_python" \
    python3.14 \
    python3.13 \
    python3.12 \
    python3.11 \
    python3.10 \
    python3
do
    [[ -n "$candidate" ]] || continue
    if resolved_candidate="$(resolve_executable "$candidate")"; then
        resolved="$resolved_candidate"
        break
    fi
done

if [[ -z "$resolved" ]]; then
    printf '%s\n' 'tmux-deep-history requires Python 3.10 or newer' >&2
    exit 1
fi

export TMUX_DEEP_HISTORY_PYTHON="$resolved"
export TMUX_DEEP_HISTORY_ROOT="$plugin_root"
if [[ -n "${PYTHONPATH:-}" ]]; then
    export PYTHONPATH="$plugin_root/src:$PYTHONPATH"
else
    export PYTHONPATH="$plugin_root/src"
fi

# Validate and enter the module in one interpreter process. The upstream
# launcher performs a separate version probe first, which can exceed the
# logger's 1.5-second readiness deadline on macOS when repeated through this
# compatibility layer.
python_program='
import runpy
import shlex
import sys
import time
from pathlib import Path

if sys.version_info < (3, 10):
    print("tmux-deep-history requires Python 3.10 or newer", file=sys.stderr)
    raise SystemExit(1)

# tmux may return from pipe-pane just before #{pane_pipe} reflects the new
# consumer. Upstream 0.1.0 checks immediately and can tear down a healthy
# logger during that small window, so make start_pipe wait for visibility.
from tmux_deep_history.tmux import PaneInfo
from tmux_deep_history.tmux import Tmux
from tmux_deep_history.tmux import TmuxError

_start_pipe = Tmux.start_pipe


def _show_global_option_batched(self, name, default=""):
    options = getattr(self, "_codexfarm_global_options", None)
    if options is None:
        options = {}
        completed = self.run("show-options", "-g", check=False)
        if completed.returncode == 0:
            for line in str(completed.stdout).splitlines():
                option_name, separator, encoded_value = line.partition(" ")
                if not separator or not option_name.startswith("@"):
                    continue
                try:
                    decoded = shlex.split(encoded_value, comments=False)
                    value = decoded[0] if len(decoded) == 1 else encoded_value
                except ValueError:
                    value = encoded_value
                options[option_name] = value
        self._codexfarm_global_options = options
    value = options.get(name, "")
    return value if value != "" else default


def _pane_info_batched(self, target):
    field_names = (
        "pane_id",
        "session_id",
        "session_name",
        "window_id",
        "window_index",
        "window_name",
        "pane_index",
        "pane_pid",
        "pane_current_command",
        "pane_current_path",
        "pane_title",
        "history_size",
        "history_limit",
        "history_bytes",
        "pane_pipe",
        "pane_height",
    )
    delimiter = "|codexfarm:field|"
    values = self.display(
        delimiter.join(f"#{{{field}}}" for field in field_names),
        target,
    ).split(delimiter)
    if len(values) != len(field_names) or not values[0].startswith("%"):
        target_label = target if target else "<current>"
        raise TmuxError(
            [self.binary, "display-message"],
            f"unable to resolve pane target: {target_label}",
        )

    def number(index):
        try:
            return int(values[index] or 0)
        except ValueError:
            return 0

    return PaneInfo(
        pane_id=values[0],
        session_id=values[1],
        session_name=values[2],
        window_id=values[3],
        window_index=values[4],
        window_name=values[5],
        pane_index=values[6],
        pane_pid=values[7],
        current_command=values[8],
        current_path=values[9],
        title=values[10],
        history_size=number(11),
        history_limit=number(12),
        history_bytes=number(13),
        pane_pipe=values[14] == "1",
        pane_height=number(15),
    )


def _server_identity_batched(self):
    delimiter = "|codexfarm:field|"
    values = self.display(
        f"#{{pid}}{delimiter}#{{start_time}}{delimiter}#{{socket_path}}"
    ).split(delimiter)
    if len(values) != 3:
        return "", "", ""
    return values[0], values[1], values[2]


def _start_pipe_after_tmux_settles(self, target, command, *, only_if_none=True):
    # A pane id beginning with %0 is consumed by tmux shell-command formatting
    # expansion unless the percent sign is doubled before pipe-pane receives it.
    command = command.replace(" --pane-id %", " --pane-id %%", 1)
    run_dir = None
    try:
        command_arguments = shlex.split(command)
        run_dir_index = command_arguments.index("--run-dir") + 1
        run_dir = Path(command_arguments[run_dir_index])
    except (ValueError, IndexError):
        pass
    _start_pipe(self, target, command, only_if_none=only_if_none)
    pipe_visible = False
    deadline = time.monotonic() + (5.0 if run_dir is not None else 0.75)
    while time.monotonic() < deadline:
        if run_dir is not None and (run_dir / "logger.ready").is_file():
            return
        if self.display("#{pane_pipe}", target, check=False) == "1":
            pipe_visible = True
            if run_dir is None:
                return
        elif pipe_visible:
            return
        time.sleep(0.025)


Tmux.show_global_option = _show_global_option_batched
Tmux.pane_info = _pane_info_batched
Tmux.server_identity = _server_identity_batched
Tmux.start_pipe = _start_pipe_after_tmux_settles
sys.argv[0] = "tmux-deep-history"
runpy.run_module("tmux_deep_history.cli", run_name="__main__")
'
python_arguments=(-c "$python_program" "$@")
if [[ "$viewer_tty_muted" -eq 1 ]]; then
    command_status=0
    "$resolved" "${python_arguments[@]}" || command_status=$?
    restore_viewer_tty
    trap - EXIT
    exit "$command_status"
fi
exec "$resolved" "${python_arguments[@]}"
