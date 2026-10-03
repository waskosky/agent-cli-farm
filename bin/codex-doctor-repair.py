#!/usr/bin/env python3
"""Apply reversible farm repairs before the doctor's final health checks."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

UNITS = ("codex-autosave.service", "codex-autosave.timer", "codex-autorestore.service")
FALSE = {"0", "false", "no", "off"}


class Repair:
    def __init__(self, source: Path | None, install: Path, session: str, manifest: Path):
        self.source, self.install, self.session, self.manifest = source, install, session, manifest
        self.state = (
            Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "codexfarm"
        )
        self.config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        self.backup: Path | None = None
        self.issues = 0
        self.monitor_changed = False

    def report(self, message: str, *, failed: bool = False) -> None:
        print(f"[{'FAIL' if failed else 'FIXED'}] {message}", flush=True)
        self.issues += int(failed)

    def preserve(self, path: Path, relative: str) -> None:
        if not path.exists() and not path.is_symlink():
            return
        if self.backup is None:
            parent = self.state / "doctor-repairs"
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.backup = Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%dT%H%M%S-"), dir=parent))
        destination = self.backup / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(path, destination, follow_symlinks=False)

    def run(self, command: list[str], *, timeout: int = 120, env: dict | None = None):
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def helpers(self) -> None:
        if self.source is None:
            return
        files = []
        for pattern in ("codex-*", "claude-*", "gemini-*", "add_high_memory_warning.sh"):
            files.extend(self.source.joinpath("bin").glob(pattern))
        files.extend(self.source.joinpath("codex_looper").glob("*.py"))
        repaired = 0
        for source in sorted(set(files)):
            if not source.is_file():
                continue
            module = source.parent.name == "codex_looper"
            relative = f"codex_looper/{source.name}" if module else source.name
            destination = self.install / relative
            content = source.read_bytes()
            if (
                destination.is_file()
                and destination.read_bytes() == content
                and (module or os.access(destination, os.X_OK))
            ):
                continue
            self.preserve(destination, f"helpers/{relative}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{source.name}.", dir=destination.parent
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                    os.fchmod(handle.fileno(), 0o644 if module else 0o755)
                os.replace(temporary, destination)
            finally:
                Path(temporary).unlink(missing_ok=True)
            repaired += 1
            if relative in {
                "codex-annotator",
                "codex-annotator.py",
                "codex_looper/health.py",
                "codex_looper/pane_status.py",
            }:
                self.monitor_changed = True
        if repaired:
            self.report(
                f"refreshed {repaired} missing, stale or non-executable installed helper(s)"
            )
        marker = self.state / "install-source"
        expected = f"{self.source.resolve()}\n"
        if not marker.is_file() or marker.read_text() != expected:
            self.preserve(marker, "install-source")
            marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, temporary = tempfile.mkstemp(prefix=".install-source-", dir=marker.parent)
            try:
                with os.fdopen(descriptor, "w") as handle:
                    handle.write(expected)
                os.replace(temporary, marker)
            finally:
                Path(temporary).unlink(missing_ok=True)

    def hooks(self) -> None:
        installer = self.install / "codex-session-hook-install.py"
        if (
            os.environ.get("CODEXFARM_INSTALL_SESSION_HOOK", "1").lower() in FALSE
            or not installer.is_file()
        ):
            return
        python = shutil.which(os.environ.get("CODEXFARM_PYTHON_BIN", "python3"))
        if python is None:
            self.report("Python is unavailable for session-hook repair", failed=True)
            return
        for provider in ("codex", "claude", "gemini"):
            if shutil.which(provider) is None:
                continue
            hooks = (
                Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "hooks.json"
                if provider == "codex"
                else Path.home() / f".{provider}" / "settings.json"
            )
            command = [str(installer), "--provider", provider, "--python-command", python]
            checked = self.run([*command, "--check"], timeout=10)
            if checked.returncode == 0:
                continue
            self.preserve(hooks, f"hooks/{provider}.json")
            if self.run(command, timeout=10).returncode:
                self.report(
                    f"{provider} session hooks could not be repaired; existing settings preserved",
                    failed=True,
                )
            else:
                self.report(f"installed current {provider} session-identity hooks")

    def capture(self) -> bool:
        if self.manifest.is_file() and self.manifest.stat().st_mode & 0o777 != 0o600:
            self.manifest.chmod(0o600)
            self.report("restored owner-only manifest permissions")
        if (
            not shutil.which("tmux")
            or self.run(["tmux", "has-session", "-t", f"={self.session}"], timeout=5).returncode
        ):
            return False
        save = os.environ.get("CODEX_SAVE_BIN", str(self.install / "codex-save"))
        result = self.run(
            [save, "--merge", str(self.manifest)], env={**os.environ, "CODEX_SESSION": self.session}
        )
        if result.returncode:
            self.report(
                "automatic manifest capture failed; existing recovery data preserved", failed=True
            )
            return False
        if not self.manifest.is_file():
            self.report("save did not produce a restore manifest", failed=True)
            return False
        self.manifest.chmod(0o600)
        # A successful unchanged capture proves freshness without creating a new snapshot.
        os.utime(self.manifest, None)
        self.report(
            "refreshed live conversation coverage; previous saved conversations and history retained"
        )
        return True

    def monitor(self) -> None:
        if any(
            os.environ.get(key, "1").lower() in FALSE
            for key in (
                "CODEX_ANNOTATOR_ENABLED",
                "CODEX_ANNOTATOR_AUTOSTART",
                "CODEXFARM_HEALTH_ENABLED",
            )
        ):
            return
        annotator = self.install / "codex-annotator"
        if not annotator.is_file() or not (self.state / "managed_sessions").exists():
            return
        try:
            health = json.loads((self.state / "health-status.json").read_text())
            age = time.time() - float(health.get("checked_at", 0))
            if 0 <= age < 60 and not self.monitor_changed:
                return
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        lock = Path(os.environ.get("CODEX_ANNOTATOR_LOCKFILE", str(self.state / "annotator.lock")))
        lock.parent.mkdir(parents=True, exist_ok=True)
        if self.monitor_changed:
            self.stop_old_monitor(lock)
        with lock.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.report(
                    "monitor heartbeat is stale while its process holds the lock", failed=True
                )
                return
        if self.run([str(annotator), "--once"], timeout=30).returncode:
            self.report(
                "memory monitor could not start; check annotator configuration", failed=True
            )
            return
        logs = self.state / "logs"
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        logfile = logs / "doctor-annotator.log"
        descriptor = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a") as output:
            subprocess.Popen(
                [str(annotator)],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                start_new_session=True,
            )
        self.report("restarted the memory/status monitor")

    def stop_old_monitor(self, lock: Path) -> None:
        """Only replace our installed annotator, proven by its argv and held lock."""
        process_fd = None
        try:
            health = json.loads((self.state / "health-status.json").read_text())
            pid = int(health.get("pid", 0))
            if pid <= 1 or pid == os.getpid():
                return
            if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
                return
            process_fd = os.pidfd_open(pid)
            proc = Path(os.environ.get("CODEX_PROC_ROOT", "/proc")) / str(pid)
            if proc.stat().st_uid != os.getuid():
                return
            argv = [
                value.decode() for value in (proc / "cmdline").read_bytes().split(b"\0") if value
            ]
            expected = (self.install / "codex-annotator.py").resolve()
            executable = Path(argv[0]).name if argv else ""
            interpreter = executable in {"python", "python3"} or (
                executable.startswith("python3.") and executable.partition(".")[2].isdigit()
            )
            if len(argv) < 2 or not interpreter or Path(argv[1]).resolve() != expected:
                return
            held = any(
                fd.resolve() == lock.resolve()
                and any(
                    line.startswith("lock:")
                    for line in (proc / "fdinfo" / fd.name).read_text().splitlines()
                )
                for fd in (proc / "fd").iterdir()
            )
            if not held:
                return
            # A pidfd prevents an exited monitor's PID from targeting another process.
            signal.pidfd_send_signal(process_fd, signal.SIGTERM)
            deadline = time.monotonic() + 3
            with lock.open("a") as handle:
                while time.monotonic() < deadline:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        return
                    except BlockingIOError:
                        time.sleep(0.05)
        except (OSError, ValueError, TypeError, AttributeError, UnicodeDecodeError):
            return
        finally:
            if process_fd is not None:
                os.close(process_fd)

    def masked(self) -> bool:
        if shutil.which("systemd-analyze"):
            result = self.run(["systemd-analyze", "--user", "unit-paths"], timeout=5)
            paths = (
                [Path(line) for line in result.stdout.splitlines()]
                if result.returncode == 0
                else []
            )
        else:
            paths = []
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
        paths += [
            self.config / "systemd/user",
            self.config / "systemd/user.control",
            runtime / "systemd/user",
            runtime / "systemd/user.control",
            runtime / "systemd/transient",
            runtime / "systemd/generator.early",
            runtime / "systemd/generator",
            runtime / "systemd/generator.late",
            data / "systemd/user",
            Path("/etc/systemd/user"),
            Path("/run/systemd/user"),
            Path("/usr/local/lib/systemd/user"),
            Path("/usr/lib/systemd/user"),
            Path("/lib/systemd/user"),
        ]
        paths += [
            Path(value) for value in os.environ.get("SYSTEMD_UNIT_PATH", "").split(":") if value
        ]
        for key, default in (
            ("XDG_CONFIG_DIRS", "/etc/xdg"),
            ("XDG_DATA_DIRS", "/usr/local/share:/usr/share"),
        ):
            paths += [
                Path(value) / "systemd/user"
                for value in os.environ.get(key, default).split(":")
                if value
            ]
        for unit in UNITS:
            for directory in paths:
                path = directory / unit
                if path.exists() and (
                    path.resolve() == Path("/dev/null")
                    or (path.is_file() and path.stat().st_size == 0)
                ):
                    return True
            result = self.run(
                [
                    "systemctl",
                    "--user",
                    "show",
                    unit,
                    "--property=LoadState",
                    "--property=UnitFileState",
                    "--value",
                ],
                timeout=5,
            )
            if any(
                line.strip() in {"masked", "masked-runtime"} for line in result.stdout.splitlines()
            ):
                return True
        return False

    def services(self, captured: bool) -> None:
        choice = self.state / "autoservice_choice"
        if choice.is_file() and choice.read_text().strip() == "no":
            return
        if not shutil.which("systemctl") or self.masked():
            return

        def state(action: str, unit: str) -> bool:
            return (
                self.run(["systemctl", "--user", action, "--quiet", unit], timeout=5).returncode
                == 0
            )

        if not state("is-enabled", "codex-autosave.timer"):
            return
        if not state("is-active", "codex-autosave.timer"):
            if self.run(
                ["systemctl", "--user", "start", "codex-autosave.timer"], timeout=10
            ).returncode:
                self.report("enabled autosave timer could not be started", failed=True)
            else:
                self.report("started the enabled autosave timer")
        if captured and state("is-failed", "codex-autosave.service"):
            # Never activate a legacy archive command without the separate opt-in.
            unit = self.run(
                [
                    "systemctl",
                    "--user",
                    "show",
                    "codex-autosave.service",
                    "--property=ExecStart",
                    "--value",
                ],
                timeout=5,
            ).stdout
            consent = self.state / "conversation_backup_choice"
            if "--archive" in unit and (
                not consent.is_file() or consent.read_text().strip() != "yes"
            ):
                self.report(
                    "legacy archive service requires a manifest-only unit refresh; archive consent preserved",
                    failed=True,
                )
                return
            result = self.run(["systemctl", "--user", "start", "codex-autosave.service"])
            if result.returncode:
                self.report("autosave service still fails after repair", failed=True)
            else:
                self.report("reran the autosave service successfully")

    def apply(self) -> int:
        for step in (self.helpers, self.hooks):
            try:
                step()
            except (OSError, ValueError, subprocess.SubprocessError):
                self.report(
                    f"{step.__name__} repair could not finish; existing files preserved",
                    failed=True,
                )
        captured = False
        try:
            captured = self.capture()
        except (OSError, ValueError, subprocess.SubprocessError):
            self.report(
                "manifest repair could not finish; existing recovery data preserved", failed=True
            )
        for step in (self.monitor, lambda: self.services(captured)):
            try:
                step()
            except (OSError, ValueError, subprocess.SubprocessError):
                self.report("background-service repair could not finish", failed=True)
        return int(self.issues > 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--install-dir", type=Path, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    return Repair(args.source, args.install_dir, args.session, args.manifest).apply()


if __name__ == "__main__":
    sys.exit(main())
