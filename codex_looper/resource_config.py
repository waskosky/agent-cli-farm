"""Strict opt-in resource settings and owner-only atomic JSON storage."""

from __future__ import annotations

import json
import os
import secrets
import stat
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .health import config_directory
from .resource_policy import HeadroomSettings, finite_number

MAX_JSON_BYTES = 65536


def _validate_stat(info: os.stat_result, *, directory: bool) -> None:
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if info.st_uid != os.getuid() or not expected(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("resource storage must be owner-only, owned, and regular")


def private_directory(path: Path, *, create: bool = False) -> Path:
    """Validate just the designated private directory, never chmod the user's home."""
    path = Path(path)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _validate_stat(path.lstat(), directory=True)
    return path


def _directory_fd(path: Path) -> int:
    private_directory(path)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _validate_stat(os.fstat(fd), directory=True)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for name, item in pairs:
        if name in value:
            raise ValueError("duplicate resource JSON field")
        value[name] = item
    return value


def read_private_json(path: Path) -> dict:
    directory = _directory_fd(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            _validate_stat(os.fstat(stream.fileno()), directory=False)
            data = stream.read(MAX_JSON_BYTES + 1)
        if len(data.encode("utf-8")) > MAX_JSON_BYTES:
            raise ValueError("resource JSON exceeds size limit")
        value = json.loads(data, object_pairs_hook=_no_duplicates)
        if not isinstance(value, dict):
            raise ValueError("resource JSON must be an object")
        return value
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if isinstance(exc, FileNotFoundError):
            raise
        raise ValueError("resource JSON is inaccessible or invalid") from exc
    finally:
        os.close(directory)


def write_private_json(path: Path, value: dict) -> Path:
    """Atomically replace a private file, refusing existing unsafe paths."""
    data = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n"
    if len(data.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("resource JSON exceeds size limit")
    private_directory(path.parent, create=True)
    directory = _directory_fd(path.parent)
    temporary = f".{path.name}.{secrets.token_hex(16)}.tmp"
    try:
        try:
            _validate_stat(
                os.stat(path.name, dir_fd=directory, follow_symlinks=False), directory=False
            )
        except FileNotFoundError:
            pass
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)
    return path


@dataclass(frozen=True)
class ResourceSettings:
    protect_agents: bool = False
    queue_background: bool = False
    investigator: str = "off"
    automatic_actions: bool = False
    reserve_mib: float = 1024
    recovery_mib: float = 1536
    recovery_seconds: float = 30
    queue_timeout: float = 300

    def __post_init__(self) -> None:
        for name in ("protect_agents", "queue_background", "automatic_actions"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be boolean")
        if self.investigator not in ("off", "codex"):
            raise ValueError("investigator must be off or codex")
        for name in ("reserve_mib", "recovery_mib", "recovery_seconds", "queue_timeout"):
            value = getattr(self, name)
            ceiling = 1_000_000_000 if name.endswith("mib") else 86400
            if not finite_number(value) or not 0 <= value <= ceiling:
                raise ValueError(f"{name} must be finite and within 0..{ceiling}")
        self.headroom()

    def headroom(self) -> HeadroomSettings:
        return HeadroomSettings(self.reserve_mib, self.recovery_mib, self.recovery_seconds)

    @classmethod
    def from_dict(cls, value: dict) -> ResourceSettings:
        if not isinstance(value, dict) or set(value) - {field.name for field in fields(cls)}:
            raise ValueError("unknown resource settings fields")
        return cls(**value)

    def as_dict(self) -> dict:
        return asdict(self)


def settings_path() -> Path:
    return config_directory() / "resource-settings.json"


def load_settings(path: Path | None = None) -> ResourceSettings:
    path = settings_path() if path is None else Path(path)
    try:
        path.lstat()
        return ResourceSettings.from_dict(read_private_json(path))
    except FileNotFoundError:
        return ResourceSettings()


def write_settings(settings: ResourceSettings, path: Path | None = None) -> Path:
    if not isinstance(settings, ResourceSettings):
        raise ValueError("expected ResourceSettings")
    path = settings_path() if path is None else Path(path)
    try:
        info = path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("settings directory must be owned and not a symlink")
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if os.fstat(fd).st_uid != os.getuid():
                raise ValueError("settings directory owner changed")
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)
    except FileNotFoundError:
        pass
    return write_private_json(path, settings.as_dict())
