"""Run AirLock DB from a source checkout without installing the package."""

from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from airlock_db.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
