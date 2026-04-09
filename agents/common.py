"""
Shared helpers for DEWD agents.
Centralised here to avoid drift between agents.
"""
import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from logger import get_logger  # noqa: F401 — re-exported for agents

ET = ZoneInfo("America/New_York")


def atomic_write(path: str, data: dict) -> None:
    """Write JSON atomically — temp file + rename prevents blank files on kill."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def write_status(output_file: str, state: str) -> None:
    """Set the status field in an agent's output JSON without touching other fields."""
    try:
        existing = {}
        if os.path.exists(output_file):
            with open(output_file) as f:
                existing = json.load(f)
        existing["status"] = state
        atomic_write(output_file, existing)
    except Exception:
        pass


def write_error(output_file: str, exc: Exception) -> None:
    """Stamp an agent output file with error status + message on failure."""
    try:
        existing = {}
        if os.path.exists(output_file):
            with open(output_file) as f:
                existing = json.load(f)
        existing.update({
            "status": "error",
            "error":  str(exc),
            "ran_at": datetime.now(timezone.utc).isoformat(),
        })
        atomic_write(output_file, existing)
    except Exception:
        pass
