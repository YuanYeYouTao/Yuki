"""Offline entrypoint kept outside the runtime plugin lifecycle."""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    raise SystemExit(import_module("github_monitor.doctor").main())
