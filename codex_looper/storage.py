"""Physical-volume disk admission and cooperative writer checkpoints.

Reservations cover peak additional bytes, not quotas or allocated storage.
Windows monitoring is independent of WSL; unknown measurements never admit
enrolled bulk work. Interactive agents are outside this policy.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .health import state_directory
from .resource_config import read_private_json, write_private_json
from .resource_policy import finite_number

GIB = 1024**3
MAX_BYTES = 1024**5


@dataclass(frozen=True)
class StorageSettings:
    enabled: bool = False
    warning_gib: float = 60
    reserve_gib: float = 25
    emergency_gib: float = 10
    recovery_gib: float = 40
    recovery_seconds: float = 120
    windows_snapshot: str | None = None

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("storage enabled must be boolean")
        for key in (
            "warning_gib",
            "reserve_gib",
            "emergency_gib",
            "recovery_gib",
            "recovery_seconds",
        ):
            value = getattr(self, key)
            if not finite_number(value) or not 0 < value <= 1_000_000:
                raise ValueError(f"{key} must be positive and finite")
        if not self.emergency_gib < self.reserve_gib < self.recovery_gib <= self.warning_gib:
            raise ValueError("disk thresholds require emergency < reserve < recovery <= warning")
        if self.windows_snapshot is not None and (
            not isinstance(self.windows_snapshot, str)
            or not Path(self.windows_snapshot).is_absolute()
        ):
            raise ValueError("Windows snapshot must be an absolute path")


def settings_path():
    return state_directory() / "resources/storage-settings.json"


def load_settings():
    try:
        value = read_private_json(settings_path())
    except FileNotFoundError:
        return StorageSettings()
    if not isinstance(value, dict) or set(value) - set(StorageSettings.__dataclass_fields__):
        raise ValueError("unknown storage settings")
    return StorageSettings(**value)


def save_settings(settings):
    # Reuse the job store's verified, private state-directory migration.
    from .resource_jobs import JobStore

    JobStore()._prepare()
    write_private_json(settings_path(), asdict(settings))


def _is_wsl():
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower()
    except OSError:
        return False


def _mount_drive(path):
    # Resolve the real mount instead of assuming all /mnt/<letter> paths are drvfs.
    try:
        device = path.stat().st_dev
        rows = Path("/proc/self/mountinfo").read_text().splitlines()
        for row in rows:
            fields = row.split()
            sep = fields.index("-")
            if fields[2] != f"{os.major(device)}:{os.minor(device)}":
                continue
            source = fields[sep + 2]
            if len(source) >= 2 and source[0].isalpha() and source[1] == ":":
                return source[:2].upper()
    except (OSError, ValueError, IndexError):
        pass
    return None


def _snapshot(path, distro, drive, now):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
        raise ValueError("unsafe Windows storage snapshot")
    with path.open("rb") as stream:
        value = json.loads(stream.read(65537))
    stamp = value.get("sampled_at")
    if value.get("schema") != "agent-farm-storage-v1" or not finite_number(stamp):
        raise ValueError("invalid Windows storage snapshot")
    if not -5 <= now - stamp <= 30:
        raise ValueError("stale Windows storage snapshot")
    if drive is None:
        drive = value.get("distributions", {}).get(distro)
    result = _validate_windows(value["volumes"][drive], drive)
    guard_volumes = value.get("guard_volumes", [])
    if not isinstance(guard_volumes, list) or len(guard_volumes) > 26:
        raise ValueError("invalid host guard volumes")
    if guard_volumes:
        guarded = [_validate_windows(value["volumes"][key], key) for key in guard_volumes]
        result["host_guard_free"] = min(item["free"] for item in guarded)
    result["vhd_file_bytes"] = value.get("vhd_files", {}).get(distro)
    result["growth_bytes_per_second"] = value.get("growth_bytes_per_second", {}).get(drive, 0)
    return result


def _validate_windows(value, drive):
    if (
        not isinstance(drive, str)
        or len(drive) != 2
        or drive[0] not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        or drive[1] != ":"
        or type(value.get("free")) is not int
        or type(value.get("total")) is not int
        or not 0 <= value["free"] <= value["total"]
    ):
        raise ValueError("invalid Windows volume counters")
    return {"free": value["free"], "total": value["total"], "volume": "windows:" + drive}


def _windows_probe(path, settings, *, now=None):
    drive = _mount_drive(path)
    # An unrelated attached Linux disk cannot be mapped to the distro VHD.
    if drive is None and path.stat().st_dev != Path("/").stat().st_dev:
        raise ValueError("unmapped WSL filesystem")
    distro = os.environ.get("WSL_DISTRO_NAME", "")
    if settings.windows_snapshot:
        # Explicit monitor enrollment requires a fresh heartbeat. Never conceal
        # a stopped monitor by falling back to a different measurement source.
        return _snapshot(Path(settings.windows_snapshot), distro, drive, now or time.time())
    context = base64.b64encode(json.dumps({"distro": distro, "drive": drive}).encode()).decode()
    script = r"""$ErrorActionPreference='Stop'
$c=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('CONTEXT')))
$drive=$c.drive
if (!$drive) {
  if (!$c.distro) { throw 'WSL distribution identity unavailable' }
  $entries=@(Get-ChildItem HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss |
    ForEach-Object { Get-ItemProperty $_.PSPath } |
    Where-Object { $_.DistributionName -eq $c.distro })
  if ($entries.Count -ne 1) { throw 'Ambiguous distribution' }
  $drive=[IO.Path]::GetPathRoot($entries[0].BasePath).TrimEnd('\')
}
$d=[IO.DriveInfo]::new($drive+'\')
@{free=[long]$d.AvailableFreeSpace;total=[long]$d.TotalSize;drive=$drive;system_free=[long]([IO.DriveInfo]::new($env:SystemDrive)).AvailableFreeSpace} | ConvertTo-Json -Compress
""".replace("CONTEXT", context)
    executable = shutil.which("powershell.exe")
    if not executable:
        candidate = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        executable = candidate if Path(candidate).is_file() else None
    if not executable:
        raise ValueError("Windows storage probe unavailable")
    encoded = base64.b64encode(script.encode("utf-16le")).decode()
    result = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=4,
        check=True,
    )
    if len(result.stdout) > 4096:
        raise ValueError("oversized Windows storage probe")
    value = json.loads(result.stdout.decode("utf-8-sig"))
    result = _validate_windows(value, value.get("drive"))
    if type(value.get("system_free")) is not int or value["system_free"] < 0:
        raise ValueError("invalid Windows system volume")
    result["host_guard_free"] = value["system_free"]
    return result


def probe(path, settings=None):
    settings = settings or load_settings()
    try:
        path = Path(path).resolve(strict=True)
        usage = shutil.disk_usage(path)
        value = {
            "available": True,
            "filesystem_free": usage.free,
            "filesystem_total": usage.total,
            "free": usage.free,
            "total": usage.total,
            "volume": "linux:" + str(path.stat().st_dev),
            "inode_free": None,
        }
        if hasattr(os, "statvfs"):
            fs = os.statvfs(path)
            value["inode_free"] = fs.f_favail if fs.f_files else None
        if _is_wsl():
            backing = _windows_probe(path, settings)
            value.update(backing)
            value["free"] = min(usage.free, backing["free"])
        return value
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return {"available": False, "reason": "physical storage measurement unavailable or stale"}


def validate_reservation(amount):
    if type(amount) is not int or not 0 <= amount <= MAX_BYTES:
        raise ValueError("disk reservation must be integer bytes within 0..1 PiB")


def pressure(sample, settings, additional=0, *, running=False):
    validate_reservation(additional)
    if not sample.get("available"):
        return sample.get("reason", "physical storage measurement unavailable")
    if sample.get("inode_free") is not None and sample["inode_free"] < 10000:
        return "filesystem inode reserve reached"
    floor = settings.emergency_gib if running else settings.reserve_gib
    if sample.get("host_guard_free", floor * GIB) < floor * GIB:
        return "Windows system/swap volume reserve reached"
    if sample["free"] < floor * GIB + additional:
        return f"disk headroom insufficient: {sample['free'] / GIB:.1f} GiB free"
    return None


def admission_reason(record, peers):
    settings = load_settings()
    amount = record.get("reserve_disk_bytes", 0)
    if not settings.enabled and not amount:
        return None
    sample = probe(record.get("storage_path") or record["cwd"], settings)
    if not sample.get("available"):
        return pressure(sample, settings)
    record["storage_volume"] = sample["volume"]
    reserved = sum(
        peer.get("reserve_disk_bytes", 0)
        for peer in peers
        if peer.get("storage_volume") == sample["volume"]
    )
    reason = pressure(sample, settings, amount + reserved)
    # The caller holds the same private lock used for all job reservations.
    path = state_directory() / "resources/storage-gates.json"
    try:
        gates = read_private_json(path)
    except FileNotFoundError:
        gates = {}
    key, now = sample["volume"], time.time()
    gate = gates.get(key, {})
    # Hysteresis tracks actual volume pressure. An oversized queued request
    # must not continually reset recovery or starve smaller safe jobs.
    if pressure(sample, settings):
        gates[key] = {"blocked": True, "recovering_since": None}
    elif gate.get("blocked"):
        if sample["free"] - reserved < settings.recovery_gib * GIB:
            gate["recovering_since"] = None
        elif gate.get("recovering_since") is None:
            gate["recovering_since"] = now
        elif now - gate["recovering_since"] >= settings.recovery_seconds:
            gate = {}
        gates[key] = gate
        if gate:
            reason = "waiting for sustained disk recovery"
    if len(gates) > 64:
        raise ValueError("storage volume registry is full")
    write_private_json(path, gates)
    return reason


def checkpoint(path, *, additional=0, running=False):
    try:
        settings = load_settings()
    except (OSError, ValueError, TypeError):
        return "storage protection settings unavailable or invalid"
    if not settings.enabled:
        return None
    return pressure(probe(path, settings), settings, additional, running=running)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    configure = sub.add_parser("configure", help="explicitly save disk protection settings")
    configure.add_argument("--enable", action="store_true")
    configure.add_argument("--disable", action="store_true")
    configure.add_argument("--windows-snapshot")
    for key in ("warning", "reserve", "emergency", "recovery"):
        configure.add_argument("--" + key + "-gib", type=float)
    configure.add_argument("--recovery-seconds", type=float)
    logs = sub.add_parser("prune-logs", help="expire inactive terminal diagnostic mirrors")
    logs.add_argument("--apply", action="store_true")
    logs.add_argument("--days", type=int, default=30)
    logs.add_argument("--max-gib", type=int, default=1)
    lease = sub.add_parser("lease", help="reserve capacity for a cooperating live writer")
    lease.add_argument("--owner-pid", type=int, required=True)
    lease.add_argument("--path", type=Path, required=True)
    lease.add_argument("--additional-bytes", type=int, required=True)
    release = sub.add_parser("release")
    release.add_argument("--job-id", required=True)
    for action in ("status", "check"):
        cmd = sub.add_parser(action)
        cmd.add_argument("--path", type=Path, default=Path.cwd())
        cmd.add_argument("--additional-bytes", type=int, default=0)
        cmd.add_argument(
            "--running", action="store_true", help="writer checkpoint: preserve emergency headroom"
        )
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
        if args.action == "configure":
            if args.enable and args.disable:
                raise ValueError("choose enable or disable")
            values = asdict(settings)
            if args.enable or args.disable:
                values["enabled"] = args.enable
            for key in values:
                value = getattr(args, key, None)
                if value is not None:
                    values[key] = value
            settings = StorageSettings(**values)
            save_settings(settings)
            print(json.dumps(asdict(settings), sort_keys=True))
            return 0
        if args.action == "prune-logs":
            from .pane_logs import prune_logs

            print(
                json.dumps(
                    prune_logs(
                        state_directory() / "logs",
                        apply=args.apply,
                        days=args.days,
                        max_bytes=args.max_gib * GIB,
                    )
                )
            )
            return 0
        if args.action == "release":
            from .resource_jobs import JobStore, process_identity

            store = JobStore()
            record = store.read(args.job_id)
            owner = process_identity(os.getppid())
            if (
                owner is None
                or record["supervisor_pid"] != owner["pid"]
                or record["supervisor_start_ticks"] != owner["start_ticks"]
            ):
                raise ValueError("only the recorded live writer may release its lease")
            store.update(args.job_id, status="finished")
            print(json.dumps({"released": True}))
            return 0
        if args.action == "lease":
            validate_reservation(args.additional_bytes)
            if not settings.enabled:
                print(json.dumps({"enabled": False, "job_id": None}))
                return 0
            from .resource_config import load_settings as resource_settings
            from .resource_jobs import JobStore, process_identity

            owner = process_identity(args.owner_pid)
            if owner is None or owner["uid"] != os.getuid() or args.owner_pid != os.getppid():
                raise ValueError("lease owner must be the direct live caller with this UID")
            store = JobStore()
            record = store.create(
                "batch",
                ["cooperative-storage-writer"],
                str(args.path.resolve(strict=True)),
                reserve_disk_bytes=args.additional_bytes,
            )
            record = store.update(
                record["job_id"],
                storage_only=True,
                supervisor_pid=owner["pid"],
                supervisor_start_ticks=owner["start_ticks"],
            )
            reason = store.reserve_admission(record["job_id"], resource_settings())
            if reason:
                store.update(record["job_id"], status="rejected")
            print(json.dumps({"enabled": True, "job_id": record["job_id"], "reason": reason}))
            return 75 if reason else 0
        validate_reservation(args.additional_bytes)
        sample = probe(args.path, settings)
        reason = pressure(sample, settings, args.additional_bytes, running=args.running)
        print(
            json.dumps(
                {
                    **sample,
                    "enabled": settings.enabled,
                    "reason": reason,
                    "warning": bool(
                        sample.get("available")
                        and min(sample["free"], sample.get("host_guard_free", sample["free"]))
                        < settings.warning_gib * GIB
                    ),
                    "settings": asdict(settings),
                },
                sort_keys=True,
            )
        )
        return 75 if args.action == "check" and settings.enabled and reason else 0
    except (OSError, ValueError, TypeError) as exc:
        print(f"codex-storage: {exc}", file=sys.stderr)
        return 2
