"""Per-ticker error log: one bad ticker never crashes the run, it lands in errors.csv."""

from __future__ import annotations

import csv
import datetime as dt
import threading
from pathlib import Path

_LOCK = threading.Lock()
COLUMNS = ["time", "stage", "ticker", "error"]


def errors_path(data_dir: Path) -> Path:
    return data_dir / "errors.csv"


def reset(data_dir: Path) -> None:
    """Start a fresh log for this run."""
    path = errors_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, path.open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerow(COLUMNS)


def log(data_dir: Path, stage: str, ticker: str, error) -> None:
    path = errors_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(COLUMNS)
            w.writerow([dt.datetime.now().isoformat(timespec="seconds"), stage, ticker,
                        f"{type(error).__name__}: {error}" if isinstance(error, Exception)
                        else str(error)])
