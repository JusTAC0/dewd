"""
DEWD Trend Setter Agent

Scans Reddit for trending Claude AI projects, emerging AI trends,
and broader world/tech signals. Two tracks:
  - Claude/AI track  — r/ClaudeAI, r/LocalLLaMA, r/artificial, etc.
  - World signals    — r/technology, r/programming, r/Futurology, etc.

Runs on schedule (4am, 8am, 12pm, 4pm, 8pm UTC) or on-demand.
Writes results to data/agents/trend_setter.json
"""
import json
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests
import anthropic

from config import ANTHROPIC_API_KEY, DATA_DIR

SONNET_MODEL = "claude-sonnet-4-6"
AGENTS_DIR   = os.path.join(DATA_DIR, "agents")
OUTPUT_FILE  = os.path.join(AGENTS_DIR, "trend_setter.json")
CUTOFF_HOURS = 72
HEADERS      = {"User-Agent": "dewd-trend-agent/1.0 (research bot)"}

AI_SUBREDDITS = [
    "ClaudeAI", "LocalLLaMA", "artificial", "singularity",
    "ChatGPTCoding", "MachineLearning", "AItools",
    "LLMDevs", "PromptEngineering", "OpenAI",
]

WORLD_SUBREDDITS = ["technology", "programming", "Futurology", "worldnews", "science"]

# RSS feeds: (name, url, limit)
AI_RSS_FEEDS = [
    ("Hugging Face Blog",  "https://huggingface.co/blog/feed.xml",        6),
    ("arXiv cs.AI+cs.CL",  "https://arxiv.org/rss/cs.AI+cs.CL",          8),
]

WORLD_KEYWORDS = [
    "ai", "artificial intelligence", "robot", "automation", "tech", "software",
    "hardware", "startup", "science", "energy", "climate", "security", "hack",
    "breakthrough", "research", "data", "quantum", "space", "biotech",
]

SCHEDULE_HOURS = [4, 8, 12, 16, 20]


# ── Reddit ────────────────────────────────────────────────────────────────────

def _fetch_subreddit(sub: str, sort: str = "hot", limit: int = 75) -> list[dict]:
    url = f"https://www.reddit.com/r/{sub}/{sort}.json?limit={limit}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return [p["data"] for p in r.json()["data"]["children"]]
    except Exception as e:
        print(f"  [trend_setter/{sub}] {e}")
        return []


def _fetch_search(query: str, limit: int = 40) -> list[dict]:
    url = f"https://www.reddit.com/search.json?q={requests.utils.quote(query)}&sort=new&t=week&limit={limit}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return [p["data"] for p in r.json()["data"]["children"]]
    except Exception as e:
        print(f"  [trend_setter/search '{query}'] {e}")
        return []


def gather_ai_posts() -> list[dict]:
    cutoff_ts = (datetime.now(timezone.utc) - timedelta(hours=CUTOFF_HOURS)).timestamp()
    all_posts, seen = [], set()

    for sub in AI_SUBREDDITS:
        for sort in ("hot", "new"):
            all_posts.extend(_fetch_subreddit(sub, sort=sort, limit=75))
            time.sleep(0.4)

    for query in ("Claude AI project", "built with Claude", "Claude API tool",
                  "Anthropic Claude", "LLM agent", "AI assistant open source"):
        all_posts.extend(_fetch_search(query))
        time.sleep(0.4)

    results = []
    for post in all_posts:
        pid = post.get("id")
        if pid in seen or not post.get("created_utc", 0) >= cutoff_ts:
            continue
        title = post.get("title", "").lower()
        body  = post.get("selftext", "").lower()
        if not any(kw in title or kw in body
                   for kw in ("claude", "anthropic", "llm", "gpt", "ai ", "machine learning",
                               "neural", "model", "agent", "chatbot")):
            continue
        seen.add(pid)
        results.append(post)

    results.sort(key=lambda p: p.get("score", 0), reverse=True)
    return results


def gather_world_signals() -> list[dict]:
    cutoff_ts = (datetime.now(timezone.utc) - timedelta(hours=CUTOFF_HOURS)).timestamp()
    all_posts, seen = [], set()

    for sub in WORLD_SUBREDDITS:
        all_posts.extend(_fetch_subreddit(sub, sort="hot", limit=50))
        time.sleep(0.4)

    results = []
    for post in all_posts:
        pid = post.get("id")
        if pid in seen or not post.get("created_utc", 0) >= cutoff_ts:
            continue
        title = post.get("title", "").lower()
        if not any(kw in title for kw in WORLD_KEYWORDS):
            continue
        if post.get("score", 0) < 50:
            continue
        seen.add(pid)
        results.append(post)

    results.sort(key=lambda p: p.get("score", 0), reverse=True)
    return results[:30]


# ── RSS ───────────────────────────────────────────────────────────────────────

def _fetch_rss(name: str, url: str, limit: int = 8) -> list[dict]:
    try:
        r = requests.get(url, timeout=12, headers=HEADERS)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        ns   = {"atom": "http://www.w3.org/2005/Atom"}
        items = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link")  or "").strip()
            desc  = (item.findtext("description") or "").strip()[:200]
            if title and link:
                items.append({"title": title[:160], "url": link, "summary": desc, "source": name})
            if len(items) >= limit:
                break
        if not items:
            for entry in root.findall(".//atom:entry", ns):
                title   = (entry.findtext("atom:title", namespaces=ns) or "").strip()
                link_el = entry.find("atom:link", ns)
                link    = (link_el.get("href") if link_el is not None else "") or ""
                summary = (entry.findtext("atom:summary", namespaces=ns) or "").strip()[:200]
                if title and link:
                    items.append({"title": title[:160], "url": link, "summary": summary, "source": name})
                if len(items) >= limit:
                    break
        return items
    except Exception as e:
        print(f"  [trend_setter/rss] {name}: {e}")
        return []


def gather_rss_signals() -> list[dict]:
    items = []
    for name, url, limit in AI_RSS_FEEDS:
        items.extend(_fetch_rss(name, url, limit))
        time.sleep(0.4)
    return items


# ── Formatting ────────────────────────────────────────────────────────────────

def _build_summary(posts: list[dict], limit: int = 60) -> list[dict]:
    out = []
    for p in posts[:limit]:
        created = datetime.fromtimestamp(p["created_utc"], tz=timezone.utc)
        out.append({
            "title":        p.get("title", ""),
            "subreddit":    p.get("subreddit", ""),
            "score":        p.get("score", 0),
            "num_comments": p.get("num_comments", 0),
            "url":          p.get("url", ""),
            "permalink":    "https://reddit.com" + p.get("permalink", ""),
            "text_preview": p.get("selftext", "")[:200].strip(),
            "created_utc":  created.strftime("%Y-%m-%d %H:%M UTC"),
            "flair":        p.get("link_flair_text", ""),
        })
    return out


def _build_top_posts(posts: list[dict], n: int = 8) -> list[dict]:
    out = []
    for p in posts[:n]:
        thumb = None
        try:
            preview_url = p["preview"]["images"][0]["resolutions"][-1]["url"]
            thumb = preview_url.replace("&amp;", "&")
        except (KeyError, IndexError, TypeError):
            pass
        created = datetime.fromtimestamp(p["created_utc"], tz=timezone.utc)
        out.append({
            "title":     p.get("title", ""),
            "subreddit": p.get("subreddit", ""),
            "score":     p.get("score", 0),
            "comments":  p.get("num_comments", 0),
            "permalink": "https://reddit.com" + p.get("permalink", ""),
            "url":       p.get("url", ""),
            "thumbnail": thumb,
            "age":       created.strftime("%b %d %H:%M"),
            "source":    "reddit",
        })
    return out


# ── Analysis ──────────────────────────────────────────────────────────────────

_SYSTEM_CACHED = [{
    "type": "text",
    "text": "You are analyzing Reddit signals for DEWD — an AI-powered personal dashboard running on a Raspberry Pi 5. Be specific, concise, and avoid filler. Surface real signal from noise.",
    "cache_control": {"type": "ephemeral"},
}]


def _build_prompt(ai_posts: list[dict], world_posts: list[dict], rss_items: list[dict]) -> str:
    return f"""Analyze these signals from the past 72 hours.

## CLAUDE / AI TRACK (Reddit)
Sources: r/ClaudeAI, r/LocalLLaMA, r/LLMDevs, r/PromptEngineering, r/OpenAI, r/artificial, r/singularity, r/MachineLearning, r/AItools
{json.dumps(_build_summary(ai_posts, 60), indent=2)}

## WORLD / TECH SIGNALS (Reddit)
Sources: r/technology, r/programming, r/Futurology, r/worldnews, r/science (tech-filtered)
{json.dumps(_build_summary(world_posts, 30), indent=2)}

## AI RESEARCH & BLOG RSS
Sources: Hugging Face Blog, arXiv cs.AI+cs.CL
{json.dumps(rss_items, indent=2)}

---

Produce this report:

## TOP CLAUDE & AI PROJECTS (Past 72h)
3-5 real named projects/tools. For each:
- NAME — what it does (1 sentence)
- Engagement: score + comments
- Why it's notable
- Link

## EMERGING AI TRENDS
3-4 patterns showing early growth momentum.
For each: TREND — signal — why it matters.

## RESEARCH PULSE (arXiv / HuggingFace)
2-3 notable papers or blog posts from arXiv cs.AI/cs.CL or Hugging Face. Brief summary + link.

## WORLD SIGNALS
3-5 notable stories from the world/tech track worth knowing about.
For each: brief headline summary + subreddit + score. No fluff.

## CONFIDENCE NOTE
1 sentence on data coverage."""


def analyze(ai_posts: list[dict], world_posts: list[dict], rss_items: list[dict]) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=SONNET_MODEL, max_tokens=2400,
        system=_SYSTEM_CACHED,
        messages=[{"role": "user", "content": _build_prompt(ai_posts, world_posts, rss_items)}],
    )
    return msg.content[0].text


def analyze_stream(ai_posts: list[dict], world_posts: list[dict], rss_items: list[dict]):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    with client.messages.stream(
        model=SONNET_MODEL, max_tokens=2400,
        system=_SYSTEM_CACHED,
        messages=[{"role": "user", "content": _build_prompt(ai_posts, world_posts, rss_items)}],
    ) as stream:
        for text in stream.text_stream:
            yield text


# ── Scheduling ────────────────────────────────────────────────────────────────

def _write_status(state: str):
    os.makedirs(AGENTS_DIR, exist_ok=True)
    try:
        existing = {}
        if os.path.exists(OUTPUT_FILE):
            with open(OUTPUT_FILE) as f: existing = json.load(f)
        existing["status"] = state
        with open(OUTPUT_FILE, "w") as f: json.dump(existing, f, indent=2)
    except Exception:
        pass


def _next_scheduled_run() -> str:
    now   = datetime.now(timezone.utc)
    today = now.date()
    for h in sorted(SCHEDULE_HOURS):
        candidate = datetime(today.year, today.month, today.day, h, 0, 0, tzinfo=timezone.utc)
        if candidate > now:
            return candidate.isoformat()
    tomorrow = today + timedelta(days=1)
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day,
                    sorted(SCHEDULE_HOURS)[0], 0, 0, tzinfo=timezone.utc).isoformat()


def run() -> dict:
    os.makedirs(AGENTS_DIR, exist_ok=True)
    _write_status("running")
    try:
        print("  [trend_setter] gathering Reddit AI posts…")
        ai_posts    = gather_ai_posts()
        print("  [trend_setter] gathering Reddit world signals…")
        world_posts = gather_world_signals()
        print("  [trend_setter] fetching AI RSS feeds…")
        rss_items   = gather_rss_signals()

        if not ai_posts and not world_posts:
            raise RuntimeError("No data found — sources may be rate-limiting.")

        report = analyze(ai_posts, world_posts, rss_items)
        top    = _build_top_posts(ai_posts, n=5) + _build_top_posts(world_posts, n=3)

        result = {
            "status":      "ok",
            "ran_at":      datetime.now(timezone.utc).isoformat(),
            "post_count":  len(ai_posts) + len(world_posts),
            "report":      report,
            "top_posts":   top,
            "next_run":    _next_scheduled_run(),
        }
    except Exception as e:
        result = {
            "status":   "error",
            "ran_at":   datetime.now(timezone.utc).isoformat(),
            "error":    str(e),
            "report":   f"Trend analysis failed: {e}",
            "next_run": _next_scheduled_run(),
        }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(result, f, indent=2)
    return result


if __name__ == "__main__":
    print(run()["report"])
