#!/usr/bin/env python3
"""Compare every farm pane's verified conversation with a saved manifest, read-only."""

from __future__ import annotations

import argparse
import os
import re
import runpy
import shutil
import subprocess
from pathlib import Path

PROVIDERS = {"codex", "claude", "gemini"}
UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
PANE_ID = re.compile(r"%[0-9]+\Z")
PANE_PID = re.compile(r"[1-9][0-9]*\Z")
SESSION_NAME = re.compile(r"[A-Za-z0-9_.-]+\Z")
SCRIPT_DIR = Path(__file__).resolve().parent


def run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    # Capture diagnostics privately: provider helpers may include session IDs in them.
    return subprocess.run(arguments, capture_output=True, text=True, check=False, timeout=30)


def manifest_identities(manifest: Path) -> set[tuple[str, str]]:
    validator = runpy.run_path(str(SCRIPT_DIR / "codex-manifest.py"))
    rows = validator["parse_manifest"](manifest.read_bytes())
    return {
        (command, args.split()[1].lower())
        for _name, _directory, command, args in rows
        if command in PROVIDERS
    }


def enumerate_panes(session: str) -> list[tuple[str, str, str]]:
    result = run(
        [
            "tmux",
            "list-panes",
            "-s",
            "-t",
            f"={session}",
            "-F",
            "#{pane_id}\t#{pane_pid}\t#{@codexfarm_utility}",
        ]
    )
    if result.returncode:
        raise ValueError("unable to enumerate panes; check the farm session and retry")
    panes = []
    seen = set()
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if (
            len(fields) != 3
            or not PANE_ID.fullmatch(fields[0])
            or not PANE_PID.fullmatch(fields[1])
            or fields[0] in seen
        ):
            raise ValueError("invalid pane enumeration; coverage could not be verified")
        seen.add(fields[0])
        panes.append((fields[0], fields[1], fields[2]))
    if not panes:
        raise ValueError("empty pane enumeration; coverage could not be verified")
    return panes


def migration_hint(session: str) -> str:
    return (
        "Use the exact session ID currently displayed in this TUI, exit normally, then relaunch "
        f"with codex-add --session {session} /path/to/project -- resume CURRENT_SESSION_ID; "
        f"run CODEX_SESSION={session} codex-save for this manifest afterward."
    )


def audit(session: str, manifest: Path, helper: str) -> int:
    try:
        saved = manifest_identities(manifest)
    except (OSError, ValueError, ImportError):
        print("[FAIL] unable to read a valid manifest; check the saved restore data")
        return 1
    if not shutil.which(helper):
        print("[FAIL] pane inspection helper unavailable; reinstall codex-save")
        return 1
    try:
        panes = enumerate_panes(session)
    except (OSError, subprocess.TimeoutExpired):
        print("[FAIL] unable to enumerate panes; check tmux availability and retry")
        return 1
    except ValueError as error:
        print(f"[FAIL] {error}")
        return 1

    issues = 0
    providers = covered = excluded = 0
    for pane, _pid, utility in panes:
        try:
            detected = run([helper, "--inspect-provider", pane])
        except (OSError, subprocess.TimeoutExpired):
            print(f"[FAIL] pane {pane}: provider inspection failed; check codex-save and retry")
            issues += 1
            continue
        if detected.returncode == 1 and not detected.stdout.strip() and not detected.stderr.strip():
            continue
        provider = detected.stdout.strip()
        if detected.returncode != 0 or provider not in PROVIDERS:
            print(f"[FAIL] pane {pane}: provider inspection failed; check codex-save and retry")
            issues += 1
            continue
        try:
            inspected = run([helper, "--inspect-pane", pane])
        except (OSError, subprocess.TimeoutExpired):
            print(f"[FAIL] pane {pane} ({provider}): identity inspection failed; retry the audit")
            issues += 1
            providers += 1
            continue
        if (
            utility == "history-picker"
            and inspected.returncode == 5
            and not inspected.stdout
            and not inspected.stderr
        ):
            excluded += 1
            continue

        providers += 1
        if inspected.returncode == 4:
            print(
                f"[FAIL] pane {pane} ({provider}): unverified legacy binding. {migration_hint(session)}"
            )
            issues += 1
            continue
        if inspected.returncode:
            failure = "identity inspection failed; " if inspected.returncode != 1 else ""
            print(
                f"[FAIL] pane {pane} ({provider}): unknown conversation ID; "
                f"{failure}exact coverage is unverified"
            )
            if provider == "codex":
                print(f"[INFO] {migration_hint(session)}")
            issues += 1
            continue
        identity = inspected.stdout.strip().split("\t")
        if len(identity) != 2 or identity[0] != provider or not UUID.fullmatch(identity[1]):
            print(
                f"[FAIL] pane {pane} ({provider}): invalid identity inspection result; retry the audit"
            )
            issues += 1
            continue
        if (provider, identity[1].lower()) not in saved:
            print(
                f"[FAIL] pane {pane} ({provider}): verified conversation missing from manifest; "
                f"run CODEX_SESSION={session} codex-save for this manifest"
            )
            issues += 1
            continue
        covered += 1

    try:
        if set(enumerate_panes(session)) != set(panes):
            print("[FAIL] panes changed during audit; retry after pane changes have finished")
            issues += 1
    except (OSError, subprocess.TimeoutExpired, ValueError):
        print("[FAIL] unable to confirm pane enumeration after inspection; retry the audit")
        issues += 1

    summary = (
        f"{providers} provider pane(s); {covered} covered; "
        f"{excluded} idle history picker(s) excluded"
    )
    if issues:
        print(f"[INFO] live pane recovery coverage is incomplete: {summary}")
        return 1
    print(f"[OK] live pane recovery coverage is complete: {summary}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", default=os.environ.get("CODEX_SESSION", "codexfarm"))
    parser.add_argument("manifest", nargs="?", type=Path)
    args = parser.parse_args()
    if not SESSION_NAME.fullmatch(args.session):
        parser.error("invalid farm session name")
    config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "codexfarm"
    manifest = args.manifest or (
        config / "manifest.tsv"
        if args.session == "codexfarm"
        else config / "manifests" / f"{args.session}.tsv"
    )
    helper = os.environ.get("CODEX_SAVE_BIN", str(SCRIPT_DIR / "codex-save"))
    return audit(args.session, manifest, helper)


if __name__ == "__main__":
    raise SystemExit(main())
