"""Informed setup opt-in, with separate consent for standalone host preferences."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from .resource_config import load_settings, write_settings

EXPLANATION = """
Gentle memory protection gives interactive agents relative CPU/IO preference and
a shared 1 GiB MemoryLow preference for used memory. Full hierarchy protection
needs a separate Linux/systemd root step. It does not reserve empty RAM or
guarantee OOM immunity. Interactive preference may slow competing batch work.

New managed batch jobs can wait for headroom. The defaults wait at or below
1024 MiB available RAM or critical memory stalls (25%); release needs at least
1536 MiB and stalls below 10% for 30 seconds. After 5 minutes the launch times
out without starting. Existing numeric thresholds/timeouts are preserved.
Running agents are never stopped or capped by this policy.
Agent wrapping applies to future managed launches; setup does not move current agents
from their scopes or select existing sessions for protection.

This choice enables only agent protection and batch queueing. AI investigation and
automatic actions remain independent; setup invokes no model. Declining means
keep all existing settings, not uninstall protection already enabled.
"""

HOST_EXPLANATION = """
The separate system step adds dedicated ancestor MemoryLow preferences and a
small 30-second root OOM maintenance timer. Named farm user-unit drop-ins also
affect other users with those unit names; OOM changes are restricted to the
chosen UID. Other workloads may face earlier reclaim. Existing stronger or
unknown preferences and operator masks are preserved; workloads are not restarted.
Review the complete read-only host plan below before deciding about sudo.
"""


def _confirm(prompt: str) -> bool:
    while True:
        print(prompt, end=" ", flush=True)
        answer = sys.stdin.readline().strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("", "n", "no"):
            return False
        print("Please answer yes or no (Enter keeps the current settings).")


def _host_supported(uid: int) -> bool:
    return (
        sys.platform == "linux"
        and uid > 0
        and all(
            Path(path).is_file() and os.access(path, os.X_OK)
            for path in ("/usr/bin/python3", "/usr/bin/systemctl")
        )
    )


def _manual_commands(plan: list[str], apply: list[str]) -> None:
    print("System protection not applied. Review and apply manually if wanted:")
    print(f"  {shlex.join(plan)}")
    print(f"  {shlex.join(apply)}")


def main(argv: list[str] | None = None, *, helper_path: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument(
        "--with-memory-protection",
        action="store_true",
        help="Enable user options; unattended runs print the manual host step only",
    )
    choice.add_argument(
        "--without-memory-protection",
        action="store_true",
        help="Skip memory setup and preserve all existing settings",
    )
    args = parser.parse_args(argv)
    if args.without_memory_protection:
        return 0
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive and not args.with_memory_protection:
        return 0

    print(EXPLANATION.strip())
    if not args.with_memory_protection and not _confirm(
        "Enable gentle memory protection for agents and new managed batch jobs? [y/N]"
    ):
        print("Keep all existing settings; declining does not uninstall existing protection.")
        return 0
    try:
        settings = load_settings()
        write_settings(replace(settings, protect_agents=True, queue_background=True))
    except (OSError, ValueError) as exc:
        print(f"Could not save memory protection settings: {exc}", file=sys.stderr)
        return 1
    print(
        "User settings saved: agent protection and batch queueing enabled; all other options preserved."
    )

    uid = os.getuid()
    if not _host_supported(uid):
        print(
            "System protection not applied: the host step requires Linux, a non-root user, and executable /usr/bin/python3 and /usr/bin/systemctl."
        )
        return 0
    helper = (
        Path(sys.argv[0]).resolve().parent / "codex-resource-host"
        if helper_path is None
        else Path(helper_path)
    )
    common = ["--uid", str(uid), "--with-maintenance"]
    plan = ["/usr/bin/python3", "-I", str(helper), "plan", *common]
    apply = ["sudo", "/usr/bin/python3", "-I", str(helper), "apply", *common]
    if not interactive:
        _manual_commands(plan, apply)
        return 0

    print(HOST_EXPLANATION.strip(), flush=True)
    try:
        preview = subprocess.run(plan, capture_output=True, text=True, check=False)
    except OSError as exc:
        print(f"Host preview failed: {exc}", file=sys.stderr)
        _manual_commands(plan, apply)
        return 0
    print(preview.stdout, end="", flush=True)
    if preview.stderr:
        print(preview.stderr, end="", file=sys.stderr, flush=True)
    if preview.returncode:
        print("Host preview failed; no system changes attempted.")
        _manual_commands(plan, apply)
        return 0
    if not _confirm("Apply these system preferences with sudo now? [y/N]"):
        _manual_commands(plan, apply)
        return 0
    try:
        result = subprocess.run(apply, check=False)
    except OSError as exc:
        print(f"User settings saved, but host apply failed: {exc}", file=sys.stderr)
        return 1
    if result.returncode:
        print(
            "User settings saved, but host apply failed. Review the helper error before retrying; no recovery or reinstall attempted.",
            file=sys.stderr,
        )
        return 1
    print("System memory preferences applied.")
    return 0
