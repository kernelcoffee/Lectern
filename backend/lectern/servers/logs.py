"""Server log helpers (M11): retention pruning.

Minecraft rotates its console log into ``logs/`` — the live file is
``latest.log`` and older runs are gzipped as ``YYYY-MM-DD-N.log.gz``. With
retention enabled we drop rotated logs older than N days (never ``latest.log``,
which the running server holds open) — pruned at start time, which is enough to
keep the directory bounded between runs.
"""

from __future__ import annotations

import time
from pathlib import Path


def prune_logs(server_dir: Path, retention_days: int) -> int:
    """Delete rotated log files older than ``retention_days``. ``0`` (or less)
    keeps everything. Returns the number of files removed."""
    if retention_days <= 0:
        return 0
    logs_dir = server_dir / "logs"
    if not logs_dir.is_dir():
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for entry in logs_dir.iterdir():
        if entry.name == "latest.log":
            continue  # the active file — never prune
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed
