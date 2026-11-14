#!/usr/bin/env python3
"""Install and inspect the farm's per-user macOS launch agents (no sudo)."""

from __future__ import annotations

import argparse
import os
import plistlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.parsers.expat import ExpatError

SAVE_LABEL = "com.codexfarm.autosave"
RESTORE_LABEL = "com.codexfarm.autorestore"
LABELS = (SAVE_LABEL, RESTORE_LABEL)
GLOBAL_DIRS = (Path("/Library/LaunchAgents"), Path("/System/Library/LaunchAgents"))


def manager(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [os.environ.get("LAUNCHCTL_BIN", "launchctl"), *args],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if check and result.returncode:
        raise ValueError(f"launchctl {args[0]} failed; check the logged-in user service manager")
    return result


def domain() -> str:
    return f"gui/{os.getuid()}"


def agent_dir() -> Path:
    return Path.home() / "Library/LaunchAgents"


def precheck() -> None:
    """Check both jobs before writing plists, registry or consent files."""
    for target in (domain(), f"user/{os.getuid()}"):
        disabled = manager("print-disabled", target).stdout
        for label in LABELS:
            if re.search(r'"' + re.escape(label) + r'"\s*=>\s*(?:true|disabled)\b', disabled):
                raise ValueError(f"{label} is disabled; preserving operator choice")
    for label in LABELS:
        for directory in (agent_dir(), *GLOBAL_DIRS):
            path = directory / f"{label}.plist"
            if path.is_symlink():
                raise ValueError(f"{label} is symlinked; preserving operator choice")
            if not path.exists():
                continue
            if directory != agent_dir():
                raise ValueError(f"{label} has a global definition; preserving operator choice")
            try:
                with path.open("rb") as source:
                    value = plistlib.load(source)
            except (ValueError, plistlib.InvalidFileException, ExpatError) as exc:
                raise ValueError(
                    f"{label} has an invalid plist; preserving operator choice"
                ) from exc
            if not isinstance(value, dict) or value.get("Disabled"):
                raise ValueError(f"{label} is disabled or invalid; preserving operator choice")
            if value.get("Label") != label:
                raise ValueError(
                    f"{label} has an unexpected definition; preserving operator choice"
                )


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".codexfarm-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def environment() -> dict[str, str]:
    home = Path.home()
    result = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"),
        "XDG_CONFIG_HOME": os.environ.get("XDG_CONFIG_HOME", str(home / ".config")),
        "XDG_STATE_HOME": os.environ.get("XDG_STATE_HOME", str(home / ".local/state")),
        "CODEX_HOME": os.environ.get("CODEX_HOME", str(home / ".codex")),
    }
    # Deliberately do not copy arbitrary environment values into a plist.
    for key in ("CODEXFARM_MANIFEST_HISTORY_KEEP", "CODEXFARM_BACKUP_KEEP"):
        if key in os.environ:
            result[key] = os.environ[key]
    return result


def install(args: argparse.Namespace) -> None:
    precheck()
    env = environment()
    state = Path(args.state_dir)
    helper_dir = Path(__file__).resolve().parent
    save = str(Path(args.save_bin or helper_dir / "codex-save").absolute())
    backup = str(Path(args.backup_bin or helper_dir / "codex-backup").absolute())
    restore = str(Path(args.restore_bin or helper_dir / "codex-restore").absolute())
    save_argv = (
        [backup, "--archive", "--min-age", "3600"]
        if args.archives == "yes"
        else [
            save,
            "--all-registered",
            "--best-effort",
        ]
    )
    definitions = {
        SAVE_LABEL: {
            "Label": SAVE_LABEL,
            "ProgramArguments": save_argv,
            "EnvironmentVariables": env,
            "StartCalendarInterval": [{"Minute": minute} for minute in range(0, 60, 5)],
            "RunAtLoad": True,
            "Nice": 10,
            "LowPriorityIO": True,
            "Umask": 0o077,
        },
    }
    if not args.save_only:
        definitions[RESTORE_LABEL] = {
            "Label": RESTORE_LABEL,
            "ProgramArguments": [restore, "--all-registered", "--skip-active"],
            "EnvironmentVariables": env,
            "RunAtLoad": True,
            "AbandonProcessGroup": True,
            "Umask": 0o077,
        }
    loaded = []
    for label in definitions:
        current = manager("print", f"{domain()}/{label}", check=False)
        if current.returncode == 0:
            if re.search(r"^\s*pid\s*=\s*\d+", current.stdout, re.MULTILINE):
                raise ValueError(f"{label} is running; retry the refresh after it finishes")
            loaded.append(label)
    for label, definition in definitions.items():
        log = state / "logs" / f"{label}.log"
        log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not log.exists():
            atomic_write(log, b"")
        definition["StandardOutPath"] = str(log)
        definition["StandardErrorPath"] = str(log)
        atomic_write(agent_dir() / f"{label}.plist", plistlib.dumps(definition))
    atomic_write(state / "conversation_backup_choice", (args.archives + "\n").encode())
    atomic_write(state / "autoservice_choice", b"yes\n")
    for label in loaded:
        manager("bootout", f"{domain()}/{label}")
    manager("bootstrap", domain(), str(agent_dir() / f"{SAVE_LABEL}.plist"))
    manager("print", f"{domain()}/{SAVE_LABEL}")
    print("macOS autosave installed: every five minutes and after login/wake.")
    if not args.save_only:
        # RunAtLoad would immediately restore everything on bootstrap. Leave
        # this plist for the next login instead of starting conversations now.
        print("Automatic restoration installed for the next login; no restore was launched.")


def status() -> int:
    precheck()
    result = manager("print", f"{domain()}/{SAVE_LABEL}", check=False)
    if result.returncode:
        print("macOS autosave is not loaded; run codex-add --install-autosave.")
        return 1
    if re.search(r"last exit code\s*=\s*(?!0\b)-?\d+", result.stdout):
        print("macOS autosave is loaded but its last save failed; inspect the autosave log.")
        return 1
    print("macOS autosave LaunchAgent is loaded (five-minute schedule).")
    return 0


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("precheck", "install", "status"))
    parser.add_argument(
        "--state-dir",
        default=str(
            Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "codexfarm"
        ),
    )
    parser.add_argument("--save-only", action="store_true")
    parser.add_argument("--archives", choices=("yes", "no"), default="no")
    parser.add_argument("--save-bin")
    parser.add_argument("--backup-bin")
    parser.add_argument("--restore-bin")
    args = parser.parse_args()
    try:
        if args.action == "precheck":
            precheck()
        elif args.action == "install":
            install(args)
        else:
            return status()
    except (OSError, ValueError, plistlib.InvalidFileException, subprocess.TimeoutExpired) as exc:
        print(f"macOS autoservice unavailable: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
