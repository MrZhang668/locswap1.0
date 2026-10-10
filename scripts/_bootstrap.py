"""Let scripts under scripts/ import backend/app, and force UTF-8 console output on Windows."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND = PROJECT_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# On Windows a redirected stdout falls back to cp1252, and non-ASCII output
# raises UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001  non-standard streams (e.g. captured by a test runner) are fine to skip
        pass
