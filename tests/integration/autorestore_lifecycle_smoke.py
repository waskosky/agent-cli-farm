#!/usr/bin/env python3
"""Check real systemd cleanup of generated restore units using private sleeping panes."""

from __future__ import annotations

import configparser
import os
import shlex
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def run(*args: str | Path, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, text=True, capture_output=True, timeout=20, **kwargs)


def main() -> None:
    tmux = shutil.which("tmux")
    if not tmux or not shutil.which("systemd-run"):
        raise RuntimeError("tmux and systemd-run are required")
    manager = run("systemctl", "--user", "show-environment")
    if manager.returncode:
        raise RuntimeError("a working systemd user manager is required")

    with tempfile.TemporaryDirectory(prefix="codexfarm-lifecycle-") as directory:
        root = Path(directory)
        stub = root / "systemctl-stub"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o700)
        env = {
            **os.environ,
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_STATE_HOME": str(root / "state"),
            "SYSTEMCTL_BIN": str(stub),
            "CODEX_SESSION": "lifecycle-test",
        }
        for exit_code in (0, 1):
            token = uuid.uuid4().hex
            socket = f"codexfarm-lifecycle-{token}"
            unit = f"codexfarm-lifecycle-{token}.service"
            launcher = root / f"launcher-{exit_code}"
            command = f"{shlex.quote(tmux)} -L {socket} -f /dev/null"
            launcher.write_text(
                "#!/bin/sh\nset -eu\n"
                f"{command} new-session -d -s lifecycle /bin/sleep 120\n"
                f"{command} new-window -d -t lifecycle /bin/sleep 120\n"
                f"exit {exit_code}\n"
            )
            launcher.chmod(0o700)
            installed = run(
                REPO_ROOT / "bin/codex-add",
                "--install-autoservice",
                env={**env, "CODEX_RESTORE_BIN": str(launcher)},
            )
            if installed.returncode:
                raise RuntimeError(installed.stdout + installed.stderr)
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            parser.read(root / "config/systemd/user/codex-autorestore.service")
            service = parser["Service"]
            try:
                started = run(
                    "systemd-run",
                    "--user",
                    f"--unit={unit}",
                    f"--property=Type={service['Type']}",
                    f"--property=KillMode={service.get('KillMode', 'control-group')}",
                    "--wait",
                    "--quiet",
                    "--",
                    *shlex.split(service["ExecStart"]),
                )
                if started.returncode != exit_code:
                    raise RuntimeError(started.stdout + started.stderr)
                panes = run(
                    tmux,
                    "-L",
                    socket,
                    "list-panes",
                    "-a",
                    "-F",
                    "#{pane_current_command}:#{pane_dead}",
                )
                if panes.returncode or panes.stdout.splitlines() != ["sleep:0", "sleep:0"]:
                    raise RuntimeError(
                        f"sleeping panes died after launcher exit {exit_code}: "
                        + panes.stdout
                        + panes.stderr
                    )
                print(f"[OK] both panes survived restore launcher exit {exit_code}")
            finally:
                run(tmux, "-L", socket, "kill-server")
                run("systemctl", "--user", "stop", unit)
                run("systemctl", "--user", "reset-failed", unit)


if __name__ == "__main__":
    main()
