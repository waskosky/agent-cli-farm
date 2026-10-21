"""Content generations let diagnostics distinguish installed and running helpers."""

import hashlib
from pathlib import Path

GUARD_FILES = (
    "health.py",
    "component_versions.py",
    "thermal.py",
    "resource_jobs.py",
    "resource_config.py",
    "resource_policy.py",
    "resource_history.py",
)
HEALTH_FILES = GUARD_FILES


def fingerprint(names, *, root=None):
    root = Path(__file__).parent if root is None else Path(root)
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode() + b"\0")
        try:
            digest.update((root / name).read_bytes())
        except FileNotFoundError:
            digest.update(b"<missing>")
    return digest.hexdigest()[:16]
