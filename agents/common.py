"""
Shared helpers for DEWD agents.
"""
import json
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from logger import get_logger  # noqa: F401 — re-exported for agents

ET_TZ = ZoneInfo("America/New_York")

_RSS_NS = {"atom": "http://www.w3.org/2005/Atom"}


def fetch_rss(name: str, url: str, limit: int = 8,
              headers: dict | None = None) -> list[dict]:
    """Fetch an RSS or Atom feed and return up to `limit` items."""
    log = get_logger(__name__)
    try:
        r = requests.get(url, timeout=12, headers=headers or {})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        items: list[dict] = []

        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link")  or "").strip()
            desc  = (item.findtext("description") or "").strip()[:300]
            if title and link:
                items.append({"title": title[:160], "url": link,
                              "summary": desc, "source": name})
            if len(items) >= limit:
                break

        if not items:
            for entry in root.findall(".//atom:entry", _RSS_NS):
                title   = (entry.findtext("atom:title",   namespaces=_RSS_NS) or "").strip()
                link_el = entry.find("atom:link", _RSS_NS)
                link    = (link_el.get("href") if link_el is not None else "") or ""
                summary = (entry.findtext("atom:summary", namespaces=_RSS_NS) or "").strip()[:300]
                if title and link:
                    items.append({"title": title[:160], "url": link,
                                  "summary": summary, "source": name})
                if len(items) >= limit:
                    break

        return items
    except Exception as e:
        log.info("[rss] %s: %s", name, e)
        return []


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
