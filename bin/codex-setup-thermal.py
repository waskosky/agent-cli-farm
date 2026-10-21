#!/usr/bin/env python3
"""Offer Windows/WSL temperature safeguards with separate cooling consent."""

import sys
from pathlib import Path

for root in (Path(__file__).resolve().parent.parent, Path(__file__).resolve().parent):
    if (root / "codex_looper").is_dir():
        sys.path.insert(0, str(root))
        break

from codex_looper.thermal_setup import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
