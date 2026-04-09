"""
DEWD Frontier Agent


log = _get_logger(__name__)
Tech acquisition scout. Hunts the tech landscape for what DEWD could
potentially absorb — new libraries, Claude API capabilities, tools,
techniques, and frameworks gaining traction.

Evaluates all finds against dewd_manifest.json before surfacing anything.
Filters through seen.json to avoid re-reporting unchanged items.
Produces a structured, scored opportunity list for Smith to act on.

Runs 2x daily (9am, 9pm ET). Morning run triggers Smith.
Writes results to data/agents/frontier.json
"""
import hashlib
import json
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests
import anthropic

from config import ANTHROPIC_API_KEY, DATA_DIR, FRONTIER_HOURS, AGENTS_DIR
from agents.common import get_logger as _get_logger
from agents.common import atomic_write, write_status, write_error, ET as _ET

HAIKU_MODEL  = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"
OUTPUT_FILE  = os.path.join(AGENTS_DIR, "frontier.json")
MANIFEST_FILE = os.path.join(AGENTS_DIR, "dewd_manifest.json")
SEEN_FILE    = os.path.join(AGENTS_DIR, "seen.json")
HEADERS      = {
    "User-Agent": "dewd-frontier/1.0 (personal research bot)",
    "Accept":     "application/vnd.github+json",
}

# ── Source definitions ────────────────────────────────────────────────────────

DEWD_PACKAGES = [
    "anthropic", "flask", "requests", "python-dotenv",
    "werkzeug", "click", "jinja2", "pytrends", "feedparser",
]

GH_QUERIES = [
    "claude api python agent",
    "anthropic python client streaming",
    "flask python dashboard raspberry-pi",
    "python server-sent events flask",
    "raspberry pi 5 python system monitor",
    "python background scheduler lightweight",
    "self hosted AI python",
    "local LLM raspberry pi",
    "python agentic loop tool use",
    "edge AI inference ARM",
]

HN_QUERIES = [
    "anthropic claude api",
    "raspberry pi python",
    "flask python dashboard",
    "python agent framework",
    "self hosted AI",
    "local LLM",
    "python background scheduler",
    "server sent events python",
]

TECH_FEEDS = [
    # Research
    ("arXiv cs.AI",          "https://arxiv.org/rss/cs.AI",                                    6),
    ("Hugging Face Blog",    "https://huggingface.co/blog/feed.xml",                            5),
    ("Simon Willison",       "https://simonwillison.net/atom/everything/",                      6),
    ("Fast.ai Blog",         "https://www.fast.ai/index.xml",                                   4),
    ("Papers With Code",     "https://paperswithcode.com/rss.xml",                              5),
    # AI Labs
    ("Anthropic Blog",       "https://www.anthropic.com/blog.rss",                              6),
    ("OpenAI Blog",          "https://openai.com/blog/rss.xml",                                 5),
    ("Google AI Blog",       "https://blog.research.google/feeds/posts/default",               5),
    ("Meta AI Blog",         "https://ai.meta.com/blog/feed/",                                  5),
    # Journalism
    ("Ars Technica",         "http://feeds.arstechnica.com/arstechnica/index",                  6),
    ("MIT Tech Review",      "https://www.technologyreview.com/feed/",                          5),
    ("Wired",                "https://www.wired.com/feed/rss",                                  5),
    ("The Verge",            "https://www.theverge.com/rss/index.xml",                         5),
    ("IEEE Spectrum",        "https://spectrum.ieee.org/feeds/feed.rss",                        4),
    ("TLDR Tech",            "https://tldr.tech/api/rss/tech",                                  6),
    # Pi / ARM / Hardware
    ("Raspberry Pi Blog",    "https://www.raspberrypi.com/news/feed/",                          6),
    ("Jeff Geerling",        "https://www.jeffgeerling.com/blog.xml",                           4),
    ("CNX Software",         "https://www.cnx-software.com/feed/",                              5),
    ("Phoronix",             "https://www.phoronix.com/rss.php",                               5),
    # Self-hosting
    ("Home Assistant Blog",  "https://www.home-assistant.io/blog.xml",                         5),
    ("Tailscale Blog",       "https://tailscale.com/blog/index.xml",                           4),
    ("Console.dev",          "https://console.dev/tools/rss.xml",                               6),
    # Dev
    ("GitHub Changelog",     "https://github.blog/changelog/feed/",                             6),
    ("Changelog.com",        "https://changelog.com/feed",                                      5),
    ("PyCoder's Weekly",     "https://pycoders.com/feed",                                       5),
    ("LangChain Blog",       "https://blog.langchain.dev/rss/",                                 4),
    ("Lobste.rs",            "https://lobste.rs/rss",                                           8),
    ("Hacker News Top",      "https://hnrss.org/frontpage?count=20&points=100",                8),
]

TECH_SUBREDDITS = [
    "LocalLLaMA", "SelfHosted", "raspberry_pi",
    "programming", "opensource", "ClaudeAI", "homelab", "Python",
]


# ── seen.json helpers ─────────────────────────────────────────────────────────

def _load_seen() -> dict:
    try:
        with open(SEEN_FILE) as f:
            return json.load(f)
    except Exception:
        return {"frontier": {"repos": {}, "packages": {}, "articles": {}}, "smith": {}}


def _save_seen(seen: dict):
    atomic_write(SEEN_FILE, seen)


def _url_fingerprint(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()


# ── Manifest loader ───────────────────────────────────────────────────────────

def _load_manifest() -> dict:
    try:
        with open(MANIFEST_FILE) as f:
            return json.load(f)
    except Exception as e:
        log.error(f"  [frontier] manifest load failed: {e}")
        return {}


# ── RSS fetcher ───────────────────────────────────────────────────────────────

def _fetch_rss(name: str, url: str, limit: int = 6) -> list[dict]:
    try:
        r = requests.get(url, timeout=12, headers=HEADERS)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        ns   = {"atom": "http://www.w3.org/2005/Atom"}
        items = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link")  or "").strip()
            desc  = (item.findtext("description") or "").strip()[:300]
            if title and link:
                items.append({"title": title[:160], "url": link, "summary": desc, "source": name})
            if len(items) >= limit:
                break
        if not items:
            for entry in root.findall(".//atom:entry", ns):
                title   = (entry.findtext("atom:title", namespaces=ns) or "").strip()
                link_el = entry.find("atom:link", ns)
                link    = (link_el.get("href") if link_el is not None else "") or ""
                summary = (entry.findtext("atom:summary", namespaces=ns) or "").strip()[:300]
                if title and link:
                    items.append({"title": title[:160], "url": link, "summary": summary, "source": name})
                if len(items) >= limit:
                    break
        return items
    except Exception as e:
        log.info(f"  [frontier/rss] {name}: {e}")
        return []


# ── Data gathering ────────────────────────────────────────────────────────────

def _gather_rss() -> list[dict]:
    items = []
    for name, url, limit in TECH_FEEDS:
        items.extend(_fetch_rss(name, url, limit))
        time.sleep(0.3)
    return items


def _gather_github() -> list[dict]:
    results = []
    for query in GH_QUERIES:
        try:
            r = requests.get(
                "https://api.github.com/search/repositories",
                headers=HEADERS,
                params={"q": f"{query} stars:>50", "sort": "stars", "order": "desc", "per_page": 5},
                timeout=10,
            )
            r.raise_for_status()
            for item in r.json().get("items", []):
                results.append({
                    "name":        item["full_name"],
                    "url":         item["html_url"],
                    "description": (item.get("description") or "")[:200],
                    "stars":       item["stargazers_count"],
                    "updated":     item["updated_at"][:10],
                    "language":    item.get("language", ""),
                    "source":      "github",
                })
            time.sleep(0.5)
        except Exception as e:
            log.info(f"  [frontier/gh] {query}: {e}")
    # Deduplicate by URL
    seen_urls, deduped = set(), []
    for item in results:
        if item["url"] not in seen_urls:
            seen_urls.add(item["url"])
            deduped.append(item)
    return deduped[:20]


def _gather_pypi() -> list[dict]:
    packages = []
    for pkg in DEWD_PACKAGES:
        try:
            r = requests.get(f"https://pypi.org/pypi/{pkg}/json", timeout=8)
            r.raise_for_status()
            info = r.json()["info"]
            packages.append({
                "package": pkg,
                "latest":  info["version"],
                "summary": (info.get("summary") or "")[:140],
                "url":     f"https://pypi.org/project/{pkg}",
            })
            time.sleep(0.2)
        except Exception:
            pass
    return packages


def _gather_hn() -> list[dict]:
    results = []
    cutoff  = int((datetime.now(timezone.utc) - timedelta(days=14)).timestamp())
    for query in HN_QUERIES:
        try:
            r = requests.get(
                "https://hn.algolia.com/api/v1/search",
                params={
                    "query": query, "tags": "story",
                    "numericFilters": f"created_at_i>{cutoff},points>75",
                    "hitsPerPage": 4,
                },
                timeout=10,
            )
            r.raise_for_status()
            for h in r.json().get("hits", []):
                results.append({
                    "title":    h.get("title", ""),
                    "url":      h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
                    "points":   h.get("points", 0),
                    "comments": h.get("num_comments", 0),
                    "age":      h.get("created_at", "")[:10],
                    "source":   "hackernews",
                })
            time.sleep(0.3)
        except Exception as e:
            log.info(f"  [frontier/hn] {query}: {e}")
    seen_urls, deduped = set(), []
    for item in results:
        if item["url"] not in seen_urls:
            seen_urls.add(item["url"])
            deduped.append(item)
    return deduped[:15]


def _gather_reddit() -> list[dict]:
    posts = []
    for sub in TECH_SUBREDDITS:
        try:
            r = requests.get(
                f"https://www.reddit.com/r/{sub}/hot.json?limit=20",
                headers=HEADERS, timeout=10,
            )
            r.raise_for_status()
            for p in r.json()["data"]["children"]:
                d = p["data"]
                if d.get("score", 0) < 50:
                    continue
                posts.append({
                    "title":     d.get("title", "")[:160],
                    "subreddit": d.get("subreddit", ""),
                    "score":     d.get("score", 0),
                    "url":       d.get("url", ""),
                    "permalink": "https://reddit.com" + d.get("permalink", ""),
                    "source":    f"r/{sub}",
                })
            time.sleep(0.4)
        except Exception as e:
            log.info(f"  [frontier/reddit] r/{sub}: {e}")
    posts.sort(key=lambda p: p.get("score", 0), reverse=True)
    return posts[:25]


def gather() -> dict:
    log.info("  [frontier] loading manifest + seen.json…")
    manifest = _load_manifest()
    seen     = _load_seen()

    log.info("  [frontier] gathering RSS feeds…")
    rss = _gather_rss()

    log.info("  [frontier] searching GitHub…")
    github = _gather_github()

    log.info("  [frontier] checking PyPI versions…")
    packages = _gather_pypi()

    log.info("  [frontier] searching HackerNews…")
    hn = _gather_hn()

    log.info("  [frontier] gathering tech Reddit…")
    reddit = _gather_reddit()

    return {
        "manifest": manifest,
        "seen":     seen,
        "rss":      rss,
        "github":   github,
        "packages": packages,
        "hn":       hn,
        "reddit":   reddit,
    }


# ── Scoring prompt ────────────────────────────────────────────────────────────

_SYSTEM = [{
    "type": "text",
    "text": (
        "You are Frontier — DEWD's tech acquisition scout. "
        "Your job is to hunt the tech landscape for what DEWD could potentially absorb: "
        "new libraries, Claude API capabilities, tools, techniques, and frameworks gaining traction.\n\n"
        "You evaluate everything against the DEWD manifest. "
        "Only surface things that have a clear, specific connection to what DEWD actually does. "
        "Generic Python libraries with no hook into DEWD's real functionality do not qualify.\n\n"
        "Scoring system (max 17 points):\n"
        "1. Functional Specificity (0-5): Maps to a named DEWD capability "
        "(SSE streaming, Claude API, Pi 5/ARM, agent scheduler, atomic file state=5; "
        "Gmail/auth=4; ntfy=3; general Python with clear hook=2; generic=1; unrelated=0)\n"
        "2. Gap Score (0-4): No solution exists=4; known weakness=3; improvable=2; solid=1; excellent=0\n"
        "3. Compatibility (0-3): Works on ARM/Pi 5, file-based, threading, no Docker=3; "
        "minor adaptations=2; major refactor=1; incompatible=0 (disqualify)\n"
        "4. Signal Strength (0-3): Official release or multiple trusted sources=3; "
        "HN 500+ or GitHub star velocity >200/week=2; single community source=1; unverifiable=0\n"
        "5. Recency (0-2): Last 7 days=2; 7-30 days=1; older=0\n\n"
        "CVEs against DEWD dependencies: always position 0, skip scoring.\n"
        "Active watchlist items: always include regardless of score.\n"
        "Known gap match: automatic +5 to Functional Specificity.\n\n"
        "Output ONLY valid JSON. No prose, no markdown, just the JSON object."
    ),
    "cache_control": {"type": "ephemeral"},
}]


def _build_prompt(data: dict, seen: dict) -> str:
    already_seen_urls = set(data["seen"].get("frontier", {}).get("articles", {}).keys())

    rss_filtered = [
        item for item in data["rss"]
        if _url_fingerprint(item.get("url", "")) not in already_seen_urls
    ][:40]

    gh_filtered = [
        item for item in data["github"]
        if item.get("url", "") not in data["seen"].get("frontier", {}).get("repos", {})
    ][:15]

    return f"""## DEWD MANIFEST (your evaluation criteria)
{json.dumps(data['manifest'], indent=2)}

## ALREADY SEEN (skip these unless materially changed)
Packages already reported: {json.dumps(list(data['seen'].get('frontier', {}).get('packages', {}).keys()))}
Repos already reported: {json.dumps(list(data['seen'].get('frontier', {}).get('repos', {}).keys()))}

## RSS SIGNALS (new only)
{json.dumps(rss_filtered, indent=2)}

## GITHUB REPOS (new only)
{json.dumps(gh_filtered, indent=2)}

## PYPI PACKAGE VERSIONS (DEWD's dependencies)
{json.dumps(data['packages'], indent=2)}

## HACKER NEWS
{json.dumps(data['hn'], indent=2)}

## TECH REDDIT
{json.dumps(data['reddit'][:20], indent=2)}

---

Evaluate all signals against the manifest. Score each qualifying find.
Return this exact JSON structure:

{{
  "opportunities": [
    {{
      "name": "library or capability name",
      "what_it_is": "one sentence description",
      "why_dewd": "specific reason this applies to DEWD — reference exact capability",
      "effort": "low | medium | high",
      "category": "new_feature | dependency_upgrade | technique | api_capability | security",
      "score": 14,
      "score_breakdown": {{
        "functional_specificity": 4,
        "gap_score": 3,
        "compatibility": 3,
        "signal_strength": 2,
        "recency": 2
      }},
      "source_url": "https://...",
      "is_security": false,
      "is_watchlist": false,
      "known_gap_match": "gap name if applicable or null"
    }}
  ],
  "package_updates": [
    {{
      "package": "anthropic",
      "installed": "unknown",
      "latest": "0.52.0",
      "upgrade_reason": "why this matters for DEWD"
    }}
  ],
  "generated_at": "{datetime.now(timezone.utc).isoformat()}"
}}

Sort opportunities: security items first, then watchlist, then by score descending.
Only include items that score 6 or above (or are security/watchlist).
Be selective. Quality over quantity."""


# ── Claude calls ──────────────────────────────────────────────────────────────

def analyze(data: dict) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=SONNET_MODEL,
        max_tokens=5000,
        system=_SYSTEM,
        messages=[{"role": "user", "content": _build_prompt(data, data["seen"])}],
    )
    raw = msg.content[0].text.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Response was likely truncated — return whatever partial data we can extract
        return {"opportunities": [], "package_updates": []}


def analyze_stream(data: dict):
    """Yields text chunks, returns final parsed JSON via StopIteration value."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    full = ""
    with client.messages.stream(
        model=SONNET_MODEL,
        max_tokens=5000,
        system=_SYSTEM,
        messages=[{"role": "user", "content": _build_prompt(data, data["seen"])}],
    ) as stream:
        for text in stream.text_stream:
            full += text
            yield text
    raw = full.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(raw)
    except Exception:
        return {}


# ── seen.json updater ─────────────────────────────────────────────────────────

def _update_seen(seen: dict, result: dict):
    """Record everything surfaced this run into seen.json."""
    now = datetime.now(timezone.utc).isoformat()
    f_seen = seen.setdefault("frontier", {"repos": {}, "packages": {}, "articles": {}})

    for opp in result.get("opportunities", []):
        url = opp.get("source_url", "")
        if url:
            fp = _url_fingerprint(url)
            f_seen["articles"][fp] = {"title": opp.get("name"), "reported_at": now}

    for pkg in result.get("package_updates", []):
        f_seen["packages"][pkg["package"]] = {
            "last_reported_version": pkg.get("latest"),
            "reported_at": now,
        }

    _save_seen(seen)


# ── Scheduling ────────────────────────────────────────────────────────────────

def _write_status(state: str):
    os.makedirs(AGENTS_DIR, exist_ok=True)
    write_status(OUTPUT_FILE, state)


def _next_run() -> str:
    hours  = sorted(FRONTIER_HOURS)
    now_et = datetime.now(_ET)
    today  = now_et.date()
    for h in hours:
        candidate = datetime(today.year, today.month, today.day, h, 0, 0, tzinfo=_ET)
        if candidate > now_et:
            return candidate.isoformat()
    tomorrow = today + timedelta(days=1)
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, hours[0], 0, 0, tzinfo=_ET).isoformat()


# ── Entry points ──────────────────────────────────────────────────────────────

def stream_run():
    """Generator — yields progress/chunk dicts for SSE streaming."""
    os.makedirs(AGENTS_DIR, exist_ok=True)
    _write_status("running")
    try:
        yield {"msg": "Loading manifest + scanning seen.json…"}
        data = gather()
        yield {"msg": f"Evaluating {len(data['rss'])} signals against DEWD manifest…"}
        full_text = ""
        result    = {}
        gen = analyze_stream(data)
        try:
            while True:
                chunk = next(gen)
                full_text += chunk
                yield {"chunk": chunk}
        except StopIteration as e:
            result = e.value or {}

        if not result:
            raw = full_text.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            try:
                result = json.loads(raw)
            except Exception:
                result = {"opportunities": [], "package_updates": []}

        _update_seen(data["seen"], result)
        output = {
            "status":        "ok",
            "ran_at":        datetime.now(timezone.utc).isoformat(),
            "opportunities": result.get("opportunities", []),
            "package_updates": result.get("package_updates", []),
            "next_run":      _next_run(),
        }
        atomic_write(OUTPUT_FILE, output)
    except Exception as e:
        write_error(OUTPUT_FILE, e)
        yield {"error": str(e)}


def run() -> dict:
    os.makedirs(AGENTS_DIR, exist_ok=True)
    _write_status("running")
    try:
        log.info("  [frontier] gathering all tech signals…")
        data   = gather()
        log.info("  [frontier] evaluating against manifest with Sonnet…")
        result = analyze(data)
        _update_seen(data["seen"], result)
        output = {
            "status":          "ok",
            "ran_at":          datetime.now(timezone.utc).isoformat(),
            "opportunities":   result.get("opportunities", []),
            "package_updates": result.get("package_updates", []),
            "next_run":        _next_run(),
        }
    except Exception as e:
        output = {
            "status":   "error",
            "ran_at":   datetime.now(timezone.utc).isoformat(),
            "error":    str(e),
            "report":   f"Frontier failed: {e}",
            "next_run": _next_run(),
        }
    atomic_write(OUTPUT_FILE, output)
    return output


if __name__ == "__main__":
    r = run()
    for opp in r.get("opportunities", []):
        log.info(f"[{opp['score']}] {opp['name']} — {opp['why_dewd']}")
