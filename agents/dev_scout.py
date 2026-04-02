"""
DEWD Dev Scout Agent

Scans GitHub, HackerNews, PyPI, and tech RSS feeds for:
  - Improvements relevant to the DEWD stack (voice AI, STT/TTS, Claude API, Pi 5)
  - Broader tech industry pulse (major launches, security news, notable OSS)

Runs every 6 hours. Writes results to data/agents/dev_scout.json
Sends ntfy push alert when critical upgrades or high-priority findings are found.
"""
import json
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests
import anthropic

from config import ANTHROPIC_API_KEY, DATA_DIR, DEV_SCOUT_INTERVAL_HRS

try:
    from notify import send_alert as _ntfy
except Exception:
    def _ntfy(*a, **kw): return False

SONNET_MODEL = "claude-sonnet-4-6"
AGENTS_DIR   = os.path.join(DATA_DIR, "agents")
OUTPUT_FILE  = os.path.join(AGENTS_DIR, "dev_scout.json")

HEADERS = {
    "User-Agent": "dewd-dev-scout/1.0 (research bot)",
    "Accept":     "application/vnd.github+json",
}

DEWD_STACK = {
    "packages": ["anthropic", "openai-whisper", "faster-whisper", "piper-tts",
                 "openwakeword", "pvporcupine", "flask", "requests",
                 "httpx", "numpy", "torch"],
    "topics":   ["voice-assistant", "speech-to-text", "text-to-speech",
                 "wake-word", "raspberry-pi", "edge-ai", "local-llm",
                 "on-device-ai", "llm-inference", "whisper-cpp",
                 "ai-assistant", "speech-synthesis", "onnxruntime", "raspberry-pi-5"],
    "keywords": ["whisper", "piper tts", "wake word detection", "voice assistant raspberry pi",
                 "claude voice", "offline voice assistant", "edge speech"],
}

RSS_FEEDS = [
    ("Hacker News RSS",   "https://hnrss.org/frontpage?count=20&points=100"),
    ("Ars Technica",      "https://feeds.arstechnica.com/arstechnica/technology-lab"),
    ("The Verge",         "https://www.theverge.com/rss/index.xml"),
    ("TechCrunch AI",     "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("Real Python",       "https://realpython.com/atom.xml"),
    ("PyPI Updates",      "https://pypi.org/rss/updates.xml"),
    ("GitHub Blog",       "https://github.blog/feed/"),
    ("Python Insider",    "https://feeds.feedburner.com/PythonInsider"),
    ("InfoQ AI/ML",       "https://feed.infoq.com/ai-ml-data-eng/"),
    ("Simon Willison",    "https://simonwillison.net/atom/everything/"),
]


def _gh_search_repos(query: str, n: int = 8) -> list[dict]:
    try:
        r = requests.get(
            "https://api.github.com/search/repositories",
            headers=HEADERS,
            params={"q": query, "sort": "stars", "order": "desc", "per_page": n},
            timeout=10,
        )
        r.raise_for_status()
        return [{
            "name":        i["full_name"],
            "url":         i["html_url"],
            "description": (i.get("description") or "")[:200],
            "stars":       i["stargazers_count"],
            "updated":     i["updated_at"][:10],
            "language":    i.get("language", ""),
            "topics":      i.get("topics", [])[:6],
        } for i in r.json().get("items", [])]
    except Exception as e:
        print(f"  [dev_scout/gh] {query}: {e}")
        return []


def _gh_recently_active(query: str, days: int = 14, n: int = 6) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        r = requests.get(
            "https://api.github.com/search/repositories",
            headers=HEADERS,
            params={"q": f"{query} pushed:>{cutoff}", "sort": "updated", "order": "desc", "per_page": n},
            timeout=10,
        )
        r.raise_for_status()
        return [{
            "name":        i["full_name"],
            "url":         i["html_url"],
            "description": (i.get("description") or "")[:200],
            "stars":       i["stargazers_count"],
            "updated":     i["updated_at"][:10],
        } for i in r.json().get("items", [])]
    except Exception as e:
        print(f"  [dev_scout/gh-active] {query}: {e}")
        return []


def _pypi_latest(package: str) -> dict | None:
    try:
        r = requests.get(f"https://pypi.org/pypi/{package}/json", timeout=8)
        r.raise_for_status()
        info = r.json()["info"]
        return {
            "package": package,
            "version": info["version"],
            "summary": (info.get("summary") or "")[:150],
            "url":     info["project_urls"].get("Homepage") or f"https://pypi.org/project/{package}",
        }
    except Exception:
        return None


def _hn_search(query: str, days: int = 7, n: int = 6) -> list[dict]:
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    try:
        r = requests.get(
            "https://hn.algolia.com/api/v1/search",
            params={"query": query, "tags": "story",
                    "numericFilters": f"created_at_i>{cutoff},points>10", "hitsPerPage": n},
            timeout=10,
        )
        r.raise_for_status()
        return [{
            "title":    h.get("title", ""),
            "url":      h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
            "hn_url":   f"https://news.ycombinator.com/item?id={h['objectID']}",
            "points":   h.get("points", 0),
            "comments": h.get("num_comments", 0),
            "age":      h.get("created_at", "")[:10],
        } for h in r.json().get("hits", [])]
    except Exception as e:
        print(f"  [dev_scout/hn] {query}: {e}")
        return []


def _fetch_rss(name: str, url: str, limit: int = 8) -> list[dict]:
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "dewd-dev-scout/1.0"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        ns   = {"atom": "http://www.w3.org/2005/Atom"}
        items = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link")  or "").strip()
            if title and link:
                items.append({"title": title[:160], "url": link, "source": name})
            if len(items) >= limit:
                break
        if not items:
            for entry in root.findall(".//atom:entry", ns):
                title   = (entry.findtext("atom:title", namespaces=ns) or "").strip()
                link_el = entry.find("atom:link", ns)
                link    = (link_el.get("href") if link_el is not None else "") or ""
                if title and link:
                    items.append({"title": title[:160], "url": link, "source": name})
                if len(items) >= limit:
                    break
        return items
    except Exception as e:
        print(f"  [dev_scout/rss] {name}: {e}")
        return []


def gather() -> dict:
    print("  [dev_scout] gathering GitHub…")
    gh_trending = []
    for kw in ["voice assistant raspberry pi", "wake word detection python",
                "faster-whisper", "piper tts", "local llm voice", "claude api python",
                "whisper cpp python", "onnxruntime speech", "real-time transcription",
                "speech recognition python 2024", "edge ai inference arm",
                "raspberry pi 5 ai", "local tts python", "llm tool use agent"]:
        gh_trending.extend(_gh_recently_active(kw, days=21, n=4))
        time.sleep(0.4)

    gh_top = []
    for topic in ["voice-assistant", "speech-recognition", "text-to-speech", "edge-ai",
                  "local-llm", "on-device-ai", "llm-inference", "whisper-cpp",
                  "ai-agent", "onnxruntime", "raspberry-pi-5", "speech-synthesis",
                  "wake-word", "automatic-speech-recognition", "natural-language-processing"]:
        gh_top.extend(_gh_search_repos(f"topic:{topic} language:python", n=5))
        time.sleep(0.4)

    print("  [dev_scout] gathering HackerNews…")
    hn_posts = []
    for q in ["voice assistant", "whisper speech", "local LLM edge", "raspberry pi AI",
               "claude API", "text to speech open source", "agentic AI",
               "MCP model context protocol", "on-device LLM",
               "AI agent", "machine learning release", "open source AI",
               "speech recognition open source", "LLM tool calling", "anthropic claude",
               "real time transcription", "audio AI", "python AI library",
               "edge inference", "small language model", "voice cloning open source"]:
        hn_posts.extend(_hn_search(q, days=14, n=4))
        time.sleep(0.3)

    print("  [dev_scout] checking PyPI versions…")
    pkg_info = []
    for pkg in DEWD_STACK["packages"]:
        info = _pypi_latest(pkg)
        if info:
            pkg_info.append(info)
        time.sleep(0.2)

    print("  [dev_scout] fetching RSS tech pulse…")
    rss_items = []
    for name, url in RSS_FEEDS:
        rss_items.extend(_fetch_rss(name, url, limit=8))
        time.sleep(0.3)

    def dedup(items):
        seen, out = set(), []
        for item in items:
            if item["url"] not in seen:
                seen.add(item["url"])
                out.append(item)
        return out

    return {
        "github":   dedup(gh_trending + gh_top)[:20],
        "hn":       dedup(hn_posts)[:16],
        "packages": pkg_info,
        "rss":      dedup(rss_items)[:24],
    }


_DEWD_CONTEXT = """DEWD is a fully offline voice AI assistant running on a Raspberry Pi 5.
Architecture: wake word detection → Whisper STT → Claude API (claude-sonnet-4-6) → Piper TTS.
It is written in Python and runs as a systemd service.
The dashboard (SHIN-DEWD) is a Flask web app served on port 8080.
Current agents: trend_setter (Reddit/world trend scanner), system_analyzer (Pi health monitor), dev_scout (this agent).
Goals: keep DEWD fast and low-latency on Pi 5, improve voice quality, expand capabilities,
       stay current with the Claude API, and discover new tools that could replace or upgrade components."""

_SYSTEM_CACHED = [{
    "type": "text",
    "text": f"You are the Dev Scout agent for DEWD — an AI voice assistant project. Your job is to scan external signals and identify concrete improvements AND broader tech awareness for the project.\n\n{_DEWD_CONTEXT}",
    "cache_control": {"type": "ephemeral"},
}]


def _build_prompt(signals: dict) -> str:
    return f"""## Signals gathered

### Recently active GitHub repos
{json.dumps(signals['github'], indent=2)}

### HackerNews stories (past 14 days)
{json.dumps(signals['hn'], indent=2)}

### Current PyPI package versions for DEWD's stack
{json.dumps(signals['packages'], indent=2)}

### Tech industry RSS pulse (Ars Technica, The Verge, TechCrunch AI, HN frontpage)
{json.dumps(signals['rss'], indent=2)}

---

Produce a Dev Scout report with these sections:

## UPGRADE ALERTS
List any DEWD dependency packages where a newer major/minor version likely exists with relevant improvements.
For each: package name, what changed, why it matters for DEWD, link.
Mark CRITICAL if it affects latency, cost, or stability.

## TOP FINDS THIS CYCLE
3-5 GitHub repos or HN stories most relevant to improving DEWD.
For each: name, what it does, why it's relevant, link, one concrete way to integrate it.

## TECH PULSE
3-4 notable stories from the RSS feeds worth knowing about — major launches, security news,
industry moves, or anything affecting the AI/dev landscape. Brief summaries only.

## QUICK WINS
2-3 small, actionable improvements DEWD could implement immediately.

## WATCH LIST
2-3 projects or trends to keep monitoring.

Be specific and actionable. Separate DEWD-specific improvements from general tech awareness."""


def analyze(signals: dict) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=SONNET_MODEL, max_tokens=2400,
        system=_SYSTEM_CACHED,
        messages=[{"role": "user", "content": _build_prompt(signals)}],
    )
    return msg.content[0].text


def analyze_stream(signals: dict):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    with client.messages.stream(
        model=SONNET_MODEL, max_tokens=2400,
        system=_SYSTEM_CACHED,
        messages=[{"role": "user", "content": _build_prompt(signals)}],
    ) as stream:
        for text in stream.text_stream:
            yield text


def _atomic_write(path: str, data: dict):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _write_status(state: str):
    os.makedirs(AGENTS_DIR, exist_ok=True)
    try:
        existing = {}
        if os.path.exists(OUTPUT_FILE):
            with open(OUTPUT_FILE) as f: existing = json.load(f)
        existing["status"] = state
        _atomic_write(OUTPUT_FILE, existing)
    except Exception:
        pass


def _next_run() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=DEV_SCOUT_INTERVAL_HRS)).isoformat()


def run() -> dict:
    os.makedirs(AGENTS_DIR, exist_ok=True)
    _write_status("running")
    try:
        signals = gather()
        report  = analyze(signals)

        if "CRITICAL" in report:
            _ntfy("Dev Scout — Critical Alert",
                  "A critical upgrade or finding was detected. Check DEWD dashboard.",
                  priority="high")

        top_finds = []
        for item in signals["github"][:6]:
            top_finds.append({"title": item["name"], "subtitle": item["description"],
                               "url": item["url"], "meta": f"★ {item['stars']}  {item['updated']}", "source": "github"})
        for item in signals["hn"][:3]:
            top_finds.append({"title": item["title"], "subtitle": "",
                               "url": item["url"], "meta": f"▲ {item['points']}  💬 {item['comments']}", "source": "hn"})
        for item in signals["rss"][:3]:
            top_finds.append({"title": item["title"], "subtitle": item["source"],
                               "url": item["url"], "meta": "RSS", "source": "rss"})

        result = {
            "status":    "ok",
            "ran_at":    datetime.now(timezone.utc).isoformat(),
            "report":    report,
            "top_finds": top_finds,
            "packages":  signals["packages"],
            "next_run":  _next_run(),
        }
    except Exception as e:
        result = {
            "status":   "error",
            "ran_at":   datetime.now(timezone.utc).isoformat(),
            "error":    str(e),
            "report":   f"Dev Scout failed: {e}",
            "next_run": _next_run(),
        }

    _atomic_write(OUTPUT_FILE, result)
    return result


if __name__ == "__main__":
    print(run()["report"])
