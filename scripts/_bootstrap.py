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


def module_source(module) -> str:
    """Source text of a module — or, for a package, of every file in it.

    The static checks in verify_*.py assert that some line of code exists. When
    a module becomes a package (agents/graph.py -> agents/graph/), the code
    they look for is spread over its files; reading them all keeps the check
    about the code rather than about which file it lives in.
    """
    path = Path(module.__file__)
    if path.name != "__init__.py":
        return path.read_text(encoding="utf-8")
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(path.parent.glob("*.py")))
