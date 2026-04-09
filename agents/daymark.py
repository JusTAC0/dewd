"""
DEWD Daymark Agent


log = _get_logger(__name__)
World awareness agent. Covers news, culture, sports, business, science,
entertainment and trending topics. No tech agenda, no DEWD agenda.
Pure world orientation — what is happening on Earth right now.

Runs 3x daily (7am, 1pm, 7pm ET).
The 7am run is the morning chain anchor — triggers Frontier after completing.
Writes results to data/agents/daymark.json
"""
import json
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

from config import (
    ANTHROPIC_API_KEY, DATA_DIR,
    DAYMARK_HOURS, MORNING_CHAIN_HOUR,
    WEATHER_LOCATION,
)
from agents.common import get_logger as _get_logger
from agents.common import atomic_write, write_status, write_error, ET as _ET

import anthropic

SONNET_MODEL = "claude-sonnet-4-6"
AGENTS_DIR   = os.path.join(DATA_DIR, "agents")
OUTPUT_FILE  = os.path.join(AGENTS_DIR, "daymark.json")
HEADERS      = {"User-Agent": "dewd-daymark/1.0 (personal research bot)"}

# ── Source definitions ────────────────────────────────────────────────────────

# RSS feeds by category: (name, url, limit)
NEWS_FEEDS = [
    ("AP News",           "https://rsshub.app/apnews/topics/apf-topnews",             8),
    ("Reuters",           "https://feeds.reuters.com/reuters/topNews",                 8),
    ("BBC News",          "http://feeds.bbci.co.uk/news/rss.xml",                     8),
    ("NPR",               "https://feeds.npr.org/1001/rss.xml",                       6),
    ("Al Jazeera",        "https://www.aljazeera.com/xml/rss/all.xml",               6),
    ("Politico",          "https://rss.politico.com/politics-news.xml",               6),
    ("Axios",             "https://api.axios.com/feed/",                              6),
    ("The Hill",          "https://thehill.com/news/feed/",                           5),
    ("Euronews",          "https://feeds.feedburner.com/euronews/en/home/",           5),
    ("South China Morning Post", "https://www.scmp.com/rss/91/feed",                 5),
]

ENTERTAINMENT_FEEDS = [
    ("Variety",              "https://variety.com/feed/",                             6),
    ("Hollywood Reporter",   "https://www.hollywoodreporter.com/feed/",               6),
    ("Deadline",             "https://deadline.com/feed/",                            6),
    ("Billboard",            "https://www.billboard.com/feed/",                       5),
    ("Rolling Stone",        "https://www.rollingstone.com/feed/",                    5),
    ("Vulture",              "https://www.vulture.com/rss/index.xml",                 5),
    ("IndieWire",            "https://www.indiewire.com/feed/",                       4),
]

SPORTS_FEEDS = [
    ("ESPN",       "https://www.espn.com/espn/rss/news",      8),
    ("BBC Sport",  "http://feeds.bbci.co.uk/sport/rss.xml",   6),
]

BUSINESS_FEEDS = [
    ("CNBC",         "https://www.cnbc.com/id/100003114/device/rss/rss.html", 6),
    ("MarketWatch",  "https://feeds.marketwatch.com/marketwatch/topstories/", 6),
    ("Forbes",       "https://www.forbes.com/feeds/forbesMainRss.xml",        5),
    ("Yahoo Finance","https://finance.yahoo.com/rss/topfinstories",            5),
]

SCIENCE_FEEDS = [
    ("Science Daily",       "https://www.sciencedaily.com/rss/all.xml",                    6),
    ("Scientific American", "https://rss.sciam.com/ScientificAmerican-News",               5),
    ("NASA",                "https://www.nasa.gov/rss/dyn/breaking_news.rss",              5),
    ("Nature News",         "https://www.nature.com/news.rss",                             4),
]

ENVIRONMENT_FEEDS = [
    ("Guardian Environment", "https://www.theguardian.com/environment/rss",  5),
    ("Carbon Brief",         "https://www.carbonbrief.org/feed/",             4),
]

WORLD_SUBREDDITS = [
    "worldnews", "news", "USnews", "science",
    "sports", "movies", "television", "todayilearned", "OutOfTheLoop",
]


# ── RSS fetcher ───────────────────────────────────────────────────────────────

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
                items.append({
                    "title":   title[:160],
                    "url":     link,
                    "summary": desc,
                    "source":  name,
                })
            if len(items) >= limit:
                break
        if not items:
            for entry in root.findall(".//atom:entry", ns):
                title   = (entry.findtext("atom:title", namespaces=ns) or "").strip()
                link_el = entry.find("atom:link", ns)
                link    = (link_el.get("href") if link_el is not None else "") or ""
                summary = (entry.findtext("atom:summary", namespaces=ns) or "").strip()[:200]
                if title and link:
                    items.append({
                        "title":   title[:160],
                        "url":     link,
                        "summary": summary,
                        "source":  name,
                    })
                if len(items) >= limit:
                    break
        return items
    except Exception as e:
        log.info(f"  [daymark/rss] {name}: {e}")
        return []


def _gather_feeds(feed_list: list[tuple]) -> list[dict]:
    items = []
    for name, url, limit in feed_list:
        items.extend(_fetch_rss(name, url, limit))
        time.sleep(0.3)
    return items


# ── Reddit ────────────────────────────────────────────────────────────────────

def _fetch_subreddit(sub: str, limit: int = 25) -> list[dict]:
    url = f"https://www.reddit.com/r/{sub}/hot.json?limit={limit}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        posts = []
        for p in r.json()["data"]["children"]:
            d = p["data"]
            if d.get("score", 0) < 100:
                continue
            posts.append({
                "title":     d.get("title", "")[:160],
                "subreddit": d.get("subreddit", ""),
                "score":     d.get("score", 0),
                "url":       d.get("url", ""),
                "permalink": "https://reddit.com" + d.get("permalink", ""),
                "source":    f"r/{sub}",
            })
        return posts
    except Exception as e:
        log.info(f"  [daymark/reddit] r/{sub}: {e}")
        return []


def _gather_reddit() -> list[dict]:
    posts = []
    for sub in WORLD_SUBREDDITS:
        posts.extend(_fetch_subreddit(sub))
        time.sleep(0.4)
    posts.sort(key=lambda p: p.get("score", 0), reverse=True)
    return posts[:40]


# ── Wikipedia trending ────────────────────────────────────────────────────────

def _fetch_wikipedia_trending() -> list[dict]:
    try:
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y/%m/%d")
        url = f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/{date_str}"
        r = requests.get(url, timeout=10, headers=HEADERS)
        r.raise_for_status()
        articles = r.json().get("items", [{}])[0].get("articles", [])[:15]
        return [
            {
                "title":  a.get("article", "").replace("_", " "),
                "views":  a.get("views", 0),
                "rank":   a.get("rank", 0),
                "source": "Wikipedia Trending",
            }
            for a in articles
            if a.get("article") not in ("Main_Page", "Special:Search")
        ]
    except Exception as e:
        log.info(f"  [daymark/wikipedia] {e}")
        return []


# ── Google Trends ─────────────────────────────────────────────────────────────

def _fetch_google_trends() -> list[str]:
    try:
        from pytrends.request import TrendReq
        pt = TrendReq(hl="en-US", tz=300, timeout=(10, 25))
        df = pt.trending_searches(pn="united_states")
        return df[0].tolist()[:15]
    except Exception as e:
        log.info(f"  [daymark/trends] {e}")
        return []


# ── Weather ───────────────────────────────────────────────────────────────────

def _fetch_weather() -> str:
    try:
        r = requests.get(
            f"https://wttr.in/{requests.utils.quote(WEATHER_LOCATION)}",
            params={"format": "3"},
            timeout=8,
            headers=HEADERS,
        )
        r.raise_for_status()
        return r.text.strip()
    except Exception as e:
        log.info(f"  [daymark/weather] {e}")
        return "Weather unavailable"


# ── Data gathering ────────────────────────────────────────────────────────────

def gather() -> dict:
    log.info("  [daymark] gathering news feeds…")
    news         = _gather_feeds(NEWS_FEEDS)
    log.info("  [daymark] gathering entertainment feeds…")
    entertainment = _gather_feeds(ENTERTAINMENT_FEEDS)
    log.info("  [daymark] gathering sports feeds…")
    sports       = _gather_feeds(SPORTS_FEEDS)
    log.info("  [daymark] gathering business feeds…")
    business     = _gather_feeds(BUSINESS_FEEDS)
    log.info("  [daymark] gathering science feeds…")
    science      = _gather_feeds(SCIENCE_FEEDS)
    log.info("  [daymark] gathering environment feeds…")
    environment  = _gather_feeds(ENVIRONMENT_FEEDS)
    log.info("  [daymark] fetching Reddit world signals…")
    reddit       = _gather_reddit()
    log.info("  [daymark] fetching Wikipedia trending…")
    wiki         = _fetch_wikipedia_trending()
    log.info("  [daymark] fetching Google Trends…")
    trends       = _fetch_google_trends()
    log.info("  [daymark] fetching weather…")
    weather      = _fetch_weather()

    return {
        "news":          news,
        "entertainment": entertainment,
        "sports":        sports,
        "business":      business,
        "science":       science,
        "environment":   environment,
        "reddit":        reddit,
        "wiki_trending": wiki,
        "google_trends": trends,
        "weather":       weather,
    }


# ── Prompt ────────────────────────────────────────────────────────────────────

_SYSTEM = [{
    "type": "text",
    "text": (
        "You are Daymark — DEWD's world awareness agent. "
        "Your job is to orient the user to what is happening in the world right now. "
        "You cover news, culture, entertainment, sports, business, and science. "
        "You have no tech agenda and no DEWD agenda. "
        "You are well-read, direct, and concise. "
        "Signal is real events with real impact. "
        "Noise is celebrity gossip without cultural weight, recycled outrage, and anything "
        "that sounds like a press release. Cut the noise. Surface the signal."
    ),
    "cache_control": {"type": "ephemeral"},
}]


def _build_prompt(data: dict) -> str:
    return f"""Analyze these world signals and produce a clean daily report.

## WEATHER
{data['weather']}

## HARD NEWS
{json.dumps(data['news'][:40], indent=2)}

## BUSINESS & MARKETS
{json.dumps(data['business'][:20], indent=2)}

## SCIENCE & ENVIRONMENT
{json.dumps(data['science'][:15] + data['environment'][:10], indent=2)}

## ENTERTAINMENT & CULTURE
{json.dumps(data['entertainment'][:25], indent=2)}

## SPORTS
{json.dumps(data['sports'][:15], indent=2)}

## REDDIT WORLD PULSE (community reaction, sorted by score)
{json.dumps(data['reddit'][:30], indent=2)}

## WIKIPEDIA TRENDING (what the world is actually reading right now)
{json.dumps(data['wiki_trending'], indent=2)}

## GOOGLE TRENDS (what the US is searching right now)
{json.dumps(data['google_trends'], indent=2)}

---

Produce this report:

## WEATHER
One line. Current conditions for the configured location.

## TOP WORLD STORIES
5 most significant news stories right now. For each:
- Headline (1 sentence)
- Why it matters (1 sentence)
- Source

## BUSINESS & MARKETS
3 notable business or market developments. Brief.

## SCIENCE & ENVIRONMENT
2-3 notable findings or events. Brief.

## CULTURE & ENTERTAINMENT
3-4 notable stories from film, music, television, or broader culture.

## SPORTS
2-3 notable results or stories.

## TRENDING RIGHT NOW
What the world is actually paying attention to — Wikipedia spikes + Google Trends.
3-5 items worth noting with brief context.

## WORLD PULSE
1-2 Reddit threads showing strong community reaction to something real.

Keep it tight. Every section should be readable in under 30 seconds."""


def analyze(data: dict) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=SONNET_MODEL,
        max_tokens=4000,
        system=_SYSTEM,
        messages=[{"role": "user", "content": _build_prompt(data)}],
    )
    return msg.content[0].text


def analyze_stream(data: dict):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    with client.messages.stream(
        model=SONNET_MODEL,
        max_tokens=4000,
        system=_SYSTEM,
        messages=[{"role": "user", "content": _build_prompt(data)}],
    ) as stream:
        for text in stream.text_stream:
            yield text


# ── Scheduling helpers ────────────────────────────────────────────────────────

def _write_status(state: str):
    os.makedirs(AGENTS_DIR, exist_ok=True)
    write_status(OUTPUT_FILE, state)


def _next_run() -> str:
    hours  = sorted(DAYMARK_HOURS)
    now_et = datetime.now(_ET)
    today  = now_et.date()
    for h in hours:
        candidate = datetime(today.year, today.month, today.day, h, 0, 0, tzinfo=_ET)
        if candidate > now_et:
            return candidate.isoformat()
    tomorrow = today + timedelta(days=1)
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, hours[0], 0, 0, tzinfo=_ET).isoformat()


def is_morning_chain_run() -> bool:
    """True if this run is the morning anchor that triggers Frontier."""
    now_et = datetime.now(_ET)
    return now_et.hour == MORNING_CHAIN_HOUR


# ── Entry points ──────────────────────────────────────────────────────────────

def stream_run():
    """Generator — yields progress/chunk dicts for SSE streaming."""
    os.makedirs(AGENTS_DIR, exist_ok=True)
    _write_status("running")
    try:
        yield {"msg": "Gathering world news feeds…"}
        data = gather()
        total = sum(len(v) for v in data.values() if isinstance(v, list))
        yield {"msg": f"Analyzing {total} signals…"}
        full = ""
        for chunk in analyze_stream(data):
            full += chunk
            yield {"chunk": chunk}
        result = {
            "status":    "ok",
            "ran_at":    datetime.now(timezone.utc).isoformat(),
            "report":    full,
            "weather":   data["weather"],
            "top_stories": data["news"][:8],
            "next_run":  _next_run(),
        }
        atomic_write(OUTPUT_FILE, result)
    except Exception as e:
        write_error(OUTPUT_FILE, e)
        yield {"error": str(e)}


def run() -> dict:
    os.makedirs(AGENTS_DIR, exist_ok=True)
    _write_status("running")
    try:
        log.info("  [daymark] gathering all world signals…")
        data   = gather()
        log.info("  [daymark] analyzing with Sonnet…")
        report = analyze(data)
        result = {
            "status":      "ok",
            "ran_at":      datetime.now(timezone.utc).isoformat(),
            "report":      report,
            "weather":     data["weather"],
            "top_stories": data["news"][:8],
            "next_run":    _next_run(),
        }
    except Exception as e:
        result = {
            "status":   "error",
            "ran_at":   datetime.now(timezone.utc).isoformat(),
            "error":    str(e),
            "report":   f"Daymark failed: {e}",
            "next_run": _next_run(),
        }
    atomic_write(OUTPUT_FILE, result)
    return result


if __name__ == "__main__":
    print(run()["report"])
