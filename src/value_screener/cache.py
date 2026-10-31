"""On-disk cache with TTL: gzip JSON for API payloads, plain text for symbol files.

Every network call goes through here, so a re-run with a warm cache makes ~0 calls.
Negative results (404s) are cached too, so missing companies aren't re-requested.
"""

from __future__ import annotations

import gzip
import json
import time
from pathlib import Path

MISSING = {"__missing__": True}


def is_fresh(path: Path, ttl_days: float) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < ttl_days * 86400


def read_json(path: Path, ttl_days: float):
    """Cached object, or None when absent/stale/corrupt."""
    if not is_fresh(path, ttl_days):
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(obj, fh)
    tmp.replace(path)


def read_text(path: Path, ttl_days: float) -> str | None:
    if not is_fresh(path, ttl_days):
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
