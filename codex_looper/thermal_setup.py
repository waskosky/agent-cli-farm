"""Interactive Windows/WSL setup; unattended and declined choices change nothing."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from dataclasses import replace
from pathlib import Path, PureWindowsPath

from . import thermal
from .resource_config import read_private_json, write_private_json
from .resource_setup import _confirm

PACKAGES = (
    (
        "LibreHardwareMonitor.zip",
        "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/v0.9.6/LibreHardwareMonitor.zip",
        "086d9f1b5a99e643edc2cfaaac16051685b551e4c5ac0b32a57c58c0e529c001",
    ),
    (
        "PawnIO_setup.exe",
        "https://github.com/namazso/PawnIO.Setup/releases/download/2.2.0/PawnIO_setup.exe",
        "1f519a22e47187f70a1379a48ca604981c4fcf694f4e65b734aaa74a9fba3032",
    ),
)
COOL_GUID = "0f5a6bd1-cc7e-4635-8bdd-09d0b13df1a8"
SCRIPTS = ("install.ps1", "sensor.ps1", "cooling-profile.ps1", "setup.ps1")
EXPLANATION = """
Optional Windows/WSL thermal protection installs checksum-pinned
LibreHardwareMonitor 0.9.6 and the signed PawnIO 2.2.0 hardware driver, if needed,
with Windows administrator approval. A dedicated Windows startup task reads CPU
temperature every five seconds. A bounded one-minute watchdog may recover only
that dedicated sensor task; it never restarts workloads. A per-user WSL service displays temperatures and
protects only managed batch jobs: defaults warn at 80 C, queue at 85 C, and share
two CPUs of CPU time at 90 C, recovering below 75 C for 60 seconds. Existing
numeric settings are preserved. Missing readings queue new managed batch jobs.
Interactive agents are never capped or restarted by the batch guard. Existing
operator masks and disabled Windows tasks/drivers are preserved. Declining keeps
existing safeguards; no driver, service or power changes follow a no/Enter/EOF.
"""
COOLING_EXPLANATION = """
Separate whole-machine option: CodexFarm Cool clones your current Windows power
plan, disables CPU boost and sets maximum processor performance to 80% on both
AC and battery. This affects ALL Windows/WSL CPU work, including agents, and can
reduce performance. Your original plan is preserved for switching back. This is
an explicit exception to the normal job-only policy; sensor setup never implies
consent to whole-machine cooling.
"""


def is_wsl() -> bool:
    if sys.platform != "linux":
        return False
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower()
    except OSError:
        return False


def _nonroot() -> bool:
    return os.getuid() > 0


def scripts_directory() -> Path:
    parent = Path(__file__).resolve().parent.parent
    source = parent / "tools/thermal-windows"
    return source if source.is_dir() else parent / "codexfarm-thermal-windows"


def ps_literal(value: str) -> str:
    if any(ord(char) < 32 for char in value):
        raise ValueError("control characters in Windows setup path")
    return "'" + value.replace("'", "''") + "'"


def powershell(executable: str, code: str, *, timeout: int = 30) -> str:
    code = (
        "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); "
        "$OutputEncoding = [Console]::OutputEncoding;\n" + code
    )
    encoded = base64.b64encode(code.encode("utf-16le")).decode("ascii")
    result = subprocess.run(
        [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ],
        text=True,
        encoding="utf-8-sig",
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode:
        raise ValueError(
            "Windows setup command failed: " + (result.stderr or result.stdout)[-3000:]
        )
    return result.stdout.strip().lstrip("\ufeff")


class WindowsBridge:
    def __init__(self):
        self.exe = shutil.which("powershell.exe")
        self.wslpath = shutil.which("wslpath")
        if not self.exe or not self.wslpath:
            raise ValueError("Windows interop requires powershell.exe and wslpath in PATH")
        info = json.loads(
            powershell(
                self.exe,
                """
[ordered]@{
  local_app_data = [Environment]::GetFolderPath('LocalApplicationData')
  program_data = [Environment]::GetFolderPath('CommonApplicationData')
} | ConvertTo-Json -Compress
""",
            )
        )
        for key in ("local_app_data", "program_data"):
            if not isinstance(info.get(key), str) or not PureWindowsPath(info[key]).is_absolute():
                raise ValueError("Windows did not return absolute installation paths")
        self.sensor = self.linux_path(
            str(PureWindowsPath(info["program_data"]) / "CodexFarm/Thermal/temperature.json")
        )
        self.base = PureWindowsPath(info["local_app_data"]) / "CodexFarm/thermal-stage/0.9.6"

    def linux_path(self, value: str) -> Path:
        result = subprocess.run(
            [self.wslpath, "-u", value],
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=5,
            check=False,
        )
        path = result.stdout.strip()
        if result.returncode or not path.startswith("/") or "\n" in path:
            raise ValueError("Could not convert a Windows installation path with wslpath")
        return Path(path)

    def stage(self, sensors: bool) -> tuple[Path, str]:
        windows = str(self.base / ("setup-" + uuid.uuid4().hex))
        directory = self.linux_path(windows)
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        for name in SCRIPTS:
            shutil.copyfile(scripts_directory() / name, directory / name)
        if sensors:
            for name, url, digest in PACKAGES:
                cached = self.linux_path(str(self.base / name))
                if cached.is_file() and cached.stat().st_size <= 64 * 1024 * 1024:
                    with cached.open("rb") as stream:
                        actual = hashlib.sha256(stream.read(64 * 1024 * 1024 + 1)).hexdigest()
                    if actual == digest:
                        shutil.copyfile(cached, directory / name)
                        continue
                print(f"Fetching verified {name}...", flush=True)
                fetch_package(directory / name, url, digest)
        return directory, windows

    def script(self, windows: str, name: str, arguments: str = "", *, timeout=30) -> str:
        path = str(PureWindowsPath(windows) / name)
        return powershell(self.exe, f"& {ps_literal(path)} {arguments}", timeout=timeout)


def fetch_package(destination: Path, url: str, digest: str) -> None:
    """Bound downloads and verify pinned content before any elevation."""
    temporary = destination.with_suffix(destination.suffix + ".partial")
    size, hasher, deadline = 0, hashlib.sha256(), time.monotonic() + 120
    try:
        with urllib.request.urlopen(url, timeout=30) as response, temporary.open("xb") as output:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 64 * 1024 * 1024 or time.monotonic() > deadline:
                    raise ValueError("Windows package download exceeded its size/time budget")
                hasher.update(chunk)
                output.write(chunk)
        if hasher.hexdigest() != digest:
            raise ValueError("Windows package checksum mismatch: " + destination.name)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _manual(args) -> None:
    flags = []
    for name in ("thermal_protection", "windows_cooling"):
        if getattr(args, "with_" + name):
            flags.append("--with-" + name.replace("_", "-"))
        elif getattr(args, "without_" + name):
            flags.append("--without-" + name.replace("_", "-"))
    print("Windows thermal setup requires an interactive WSL terminal for administrator approval.")
    print("No settings or Windows services changed. Run in a terminal:")
    print("  " + shlex.join([sys.executable, sys.argv[0], *flags]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for feature in ("thermal-protection", "windows-cooling"):
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--with-" + feature, action="store_true")
        group.add_argument("--without-" + feature, action="store_true")
    args = parser.parse_args(argv)
    explicit = args.with_thermal_protection or args.with_windows_cooling
    if args.without_thermal_protection and args.without_windows_cooling:
        return 0
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        if explicit:
            _manual(args)
        return 0
    if not is_wsl() or not _nonroot():
        if explicit:
            print(
                "Windows thermal setup requires non-root WSL2 with Windows interop and systemd.",
                file=sys.stderr,
            )
            return 1
        return 0

    sensors = args.with_thermal_protection
    cooling = args.with_windows_cooling
    try:
        if not sensors and not args.without_thermal_protection:
            if thermal.settings_path().exists():
                print(
                    "Thermal safeguards already configured; existing settings and services preserved."
                )
                print("Use --with-thermal-protection to check/refresh the Windows integration.")
            else:
                print(EXPLANATION.strip())
                sensors = _confirm(
                    "Set up Windows CPU temperature monitoring and managed batch protection? [y/N]"
                )
        elif sensors:
            print(EXPLANATION.strip())
        if cooling:
            print(COOLING_EXPLANATION.strip())
        elif sensors and not args.without_windows_cooling:
            print(COOLING_EXPLANATION.strip())
            cooling = _confirm("Also enable whole-machine cooling (80% maximum, boost off)? [y/N]")
        if not sensors and not cooling:
            print("Existing thermal settings, services and Windows power choices preserved.")
            return 0

        if sensors:
            thermal.precheck_service()
            thermal.prepare_private_directory(thermal.settings_path().parent)
        # A cooling-only run still writes a private consent/undo receipt.
        thermal.prepare_private_directory(thermal.state_directory())
        settings = thermal.load_settings() if sensors else None
        bridge = WindowsBridge()
        if sensors:
            proposed = replace(settings, enabled=True, enforce=True, sensor_file=str(bridge.sensor))
            fresh = thermal.read_temperature(proposed)["cpu_celsius"] is not None
            if fresh:
                try:
                    sample = thermal.read_windows_json(bridge.sensor)
                    fresh = (
                        sample.get("helper_version") == thermal.SENSOR_VERSION
                        and sample.get("helper_sha256") == thermal.sensor_digest()
                    )
                except (OSError, ValueError, TypeError):
                    fresh = False
            print(
                f"Batch plan: warn {proposed.warning_c:g} C; queue {proposed.queue_c:g} C; "
                f"critical {proposed.critical_c:g} C, combined CPU budget {proposed.batch_budget_percent:g}%; "
                f"recover below {proposed.recovery_c:g} C for {proposed.recovery_seconds:g}s.",
                flush=True,
            )
        else:
            fresh = False
        directory, windows = bridge.stage(sensors and not fresh)
        original = ""
        needs_cooling = cooling
        if cooling:
            status = json.loads(bridge.script(windows, "cooling-profile.ps1", "-Action status"))
            original = status["active_guid"]
            if original == COOL_GUID:
                if status.get("verified") is not True:
                    raise ValueError(
                        "Existing active cooling profile was changed; operator settings preserved"
                    )
                needs_cooling = False
                print(
                    "Verified existing CodexFarm Cool power settings; preserving the active plan."
                )
            else:
                print(
                    bridge.script(
                        windows,
                        "cooling-profile.ps1",
                        "-Action plan -OriginalScheme " + ps_literal(original),
                    )
                )
        needs_sensors = sensors and not fresh
        if needs_sensors or needs_cooling:
            switches = (" -InstallSensors" if needs_sensors else "") + (
                " -Cooling" if needs_cooling else ""
            )
            if needs_cooling:
                switches += " -OriginalScheme " + ps_literal(original)
            print(
                "Approve the Windows UAC prompt for the reviewed options. Progress/errors are saved in "
                + str(directory),
                flush=True,
            )
            bridge.script(windows, "setup.ps1", switches, timeout=360)
            result = json.loads((directory / "setup-result.json").read_text(encoding="utf-8-sig"))
            if result.get("success") is not True:
                raise ValueError(
                    "Windows installation failed: " + str(result.get("error", "unknown error"))
                )
        if sensors:
            # Hardware validation must succeed before enrolling future batch work.
            if thermal.read_temperature(proposed)["cpu_celsius"] is None:
                raise ValueError("No fresh real CPU readings; thermal settings were not enabled")
            thermal.precheck_service()
            thermal.save_settings(proposed)
            thermal.install_service()
            print("Windows telemetry verified; per-user thermal guard enabled.")
        if cooling:
            status = json.loads(bridge.script(windows, "cooling-profile.ps1", "-Action status"))
            if status.get("active_guid") != COOL_GUID or status.get("verified") is not True:
                raise ValueError("Windows cooling activation could not be verified")
        receipt_path = thermal.state_directory() / "thermal-setup.json"
        try:
            receipt = read_private_json(receipt_path)
        except FileNotFoundError:
            receipt = {}
        receipt.update(checked_at=time.time(), windows_stage=windows)
        if sensors:
            receipt["thermal_enabled"] = True
        if cooling:
            receipt["cooling_approved"] = True
            if original != COOL_GUID:
                receipt["original_power_scheme"] = original
        write_private_json(receipt_path, receipt)
        if cooling and original != COOL_GUID:
            print(
                "Whole-machine cooling verified. Undo in Windows: powercfg /setactive " + original
            )
        return 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        print(
            f"Thermal setup incomplete: {exc}. Existing Windows changes are not automatically retried or undone.",
            file=sys.stderr,
        )
        return 1
