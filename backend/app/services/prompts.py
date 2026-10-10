"""Prompt loader: prompts are standalone markdown files under prompts/, decoupled from the code.

Hot-reloaded by mtime — editing a prompt requires no code change and no server restart.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import settings

logger = logging.getLogger(__name__)

_cache: dict[str, tuple[float, str]] = {}


def load_prompt(name: str) -> str:
    """`name` has no extension, e.g. 'publishing_extract'."""
    path: Path = settings.prompts_path / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"prompt file not found: {path}")
    mtime = path.stat().st_mtime
    cached = _cache.get(name)
    if cached and cached[0] == mtime:
        return cached[1]
    text = path.read_text(encoding="utf-8")
    _cache[name] = (mtime, text)
    logger.info("[prompts] loaded %s (%d chars)", path.name, len(text))
    return text
