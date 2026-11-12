"""Test bootstrap: make the vendored, offline dependencies importable."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / ".pylib" / "lib" / "python3.11" / "site-packages"
if VENDOR.exists():
    sys.path.insert(0, str(VENDOR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
