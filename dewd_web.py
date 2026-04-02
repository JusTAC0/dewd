#!/usr/bin/env python3
"""
DEWD Mission Control — served at http://<pi-ip>:8080
Text-only dashboard: chat via browser → brain.py → Claude API.
Runs background scheduler for Trend Setter, System Analyzer, Dev Scout agents.
"""
import json
import os
import time
import subprocess
import shutil
import imaplib
import requests
import email
import email.utils
import re as _re
import threading
from email.header import decode_header as _decode_header
from html.parser import HTMLParser as _HTMLParser
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
from functools import wraps
from flask import Flask, Response, jsonify, render_template_string, request, session, redirect

from config import (
    DATA_DIR, STATUS_FILE, LOG_FILE,
    GMAIL_ADDRESS, GMAIL_APP_PASSWORD, GMAIL_MAX_MSGS,
    MAX_LOG_ENTRIES,
    TREND_SETTER_START_HOUR, TREND_SETTER_INTERVAL_HRS,
    SYS_ANALYZER_START_HOUR, SYS_ANALYZER_INTERVAL_HRS,
    DEV_SCOUT_START_HOUR, DEV_SCOUT_INTERVAL_HRS,
    AGENTS_DIR,
    WEATHER_LOCATION,
    SECRET_KEY, DASHBOARD_PASSWORD,
)

try:
    from brain import DewdBrain
    _brain    = DewdBrain()
    _brain_ok = True
except Exception as _e:
    _brain    = None
    _brain_ok = False
    print(f"[web] brain unavailable: {_e}")

_brain_lock = threading.Lock()

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["SESSION_PERMANENT"] = False  # session dies when browser closes

os.makedirs(AGENTS_DIR, exist_ok=True)


# ── Auth ──────────────────────────────────────────────────────────────────────

_LOGIN_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>SHIN-DEWD · Access</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:#0e0614;color:#e0c8b0;font-family:'JetBrains Mono','Fira Mono','Courier New',monospace;
      display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}
    .box{width:100%;max-width:320px;background:rgba(20,8,28,.97);
      border:1px solid rgba(247,169,61,.22);border-radius:8px;padding:40px 28px;text-align:center}
    .logo-k{font-size:2.2rem;font-weight:900;color:#f7a93d;letter-spacing:-.02em}
    .logo-en{font-size:.85rem;letter-spacing:.2em;color:#fef4e8;margin-bottom:4px}
    .logo-sub{font-size:.6rem;letter-spacing:.15em;color:rgba(224,200,176,.4);margin-bottom:36px}
    .prompt{font-size:.65rem;letter-spacing:.12em;color:rgba(247,169,61,.7);margin-bottom:12px}
    input[type=password]{width:100%;padding:13px 14px;background:rgba(10,4,16,.9);
      border:1px solid rgba(247,169,61,.3);border-radius:4px;color:#fef4e8;
      font-family:inherit;font-size:1.4rem;letter-spacing:.3em;text-align:center;
      outline:none;margin-bottom:14px}
    input[type=password]:focus{border-color:rgba(247,169,61,.8)}
    button{width:100%;padding:12px;background:transparent;border:1px solid #f7a93d;
      border-radius:4px;color:#f7a93d;font-family:inherit;font-size:.75rem;
      font-weight:700;letter-spacing:.12em;cursor:pointer}
    button:active{background:rgba(247,169,61,.12)}
    .error{font-size:.65rem;letter-spacing:.1em;color:#ce4458;margin-top:16px}
  </style>
</head>
<body>
  <div class="box">
    <div class="logo-k">新</div>
    <div class="logo-en">SHIN-DEWD</div>
    <div class="logo-sub">MISSION CONTROL</div>
    <div class="prompt">ENTER ACCESS CODE</div>
    <form method="post">
      <input type="password" name="password" autofocus autocomplete="off" inputmode="numeric">
      <button type="submit">ACCESS</button>
      {% if error %}<div class="error">INVALID CODE — TRY AGAIN</div>{% endif %}
    </form>
  </div>
</body>
</html>"""


def _is_mobile():
    ua = request.headers.get("User-Agent", "")
    return bool(_re.search(r"Mobile|Android|iPhone|iPad|webOS|BlackBerry|IEMobile", ua, _re.I))


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if _is_mobile() or session.get("authenticated"):
            return f(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"error": "unauthorized"}), 401
        return redirect("/login")
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == DASHBOARD_PASSWORD:
            session["authenticated"] = True
            return redirect("/")
        return render_template_string(_LOGIN_HTML, error=True)
    return render_template_string(_LOGIN_HTML, error=False)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# ── Helpers ──────────────────────────────────────────────────────────────────

def _read_json(path, fallback):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return fallback


def _system_stats():
    stats = {}
    try:
        temp = subprocess.check_output(["vcgencmd", "measure_temp"], text=True).strip()
        stats["temp"] = temp.replace("temp=", "")
    except Exception:
        stats["temp"] = "—"
    try:
        with open("/proc/meminfo") as f:
            mem = {l.split(":")[0]: int(l.split()[1]) for l in f}
        total = mem["MemTotal"] // 1024
        avail = mem["MemAvailable"] // 1024
        stats["ram_used_mb"]  = total - avail
        stats["ram_total_mb"] = total
        stats["ram_pct"]      = round((total - avail) / total * 100, 1)
    except Exception:
        stats["ram_used_mb"] = stats["ram_total_mb"] = stats["ram_pct"] = 0
    try:
        du = shutil.disk_usage("/")
        stats["disk_used_gb"]  = round(du.used  / 1e9, 1)
        stats["disk_total_gb"] = round(du.total / 1e9, 1)
        stats["disk_pct"]      = round(du.used / du.total * 100, 1)
    except Exception:
        stats["disk_used_gb"] = stats["disk_total_gb"] = stats["disk_pct"] = 0
    try:
        with open("/proc/stat") as f:
            c0 = f.readline().split()
        time.sleep(0.3)
        with open("/proc/stat") as f:
            c1 = f.readline().split()
        idle0, total0 = int(c0[4]), sum(int(x) for x in c0[1:])
        idle1, total1 = int(c1[4]), sum(int(x) for x in c1[1:])
        stats["cpu_pct"] = round(100.0 * (1 - (idle1 - idle0) / (total1 - total0)), 1)
    except Exception:
        stats["cpu_pct"] = 0
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
        h, m = divmod(int(secs) // 60, 60)
        stats["uptime"] = f"{h}h {m}m"
    except Exception:
        stats["uptime"] = "—"
    try:
        with open("/proc/net/dev") as f:
            lines = f.readlines()[2:]
        ifaces = []
        for line in lines:
            parts = line.split()
            if len(parts) < 10:
                continue
            name = parts[0].rstrip(":")
            if name == "lo":
                continue
            rx_mb = round(int(parts[1]) / 1e6, 1)
            tx_mb = round(int(parts[9]) / 1e6, 1)
            ifaces.append({"name": name, "rx_mb": rx_mb, "tx_mb": tx_mb})
        stats["interfaces"] = ifaces
    except Exception:
        stats["interfaces"] = []
    return stats


# ── Gmail ─────────────────────────────────────────────────────────────────────

def _decode_str(raw):
    parts = _decode_header(raw or "")
    out = []
    for b, enc in parts:
        if isinstance(b, bytes):
            out.append(b.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(b)
    return "".join(out)


def _fetch_gmail():
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return {"configured": False, "unread": 0, "emails": []}
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=10)
        mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        mail.select("INBOX", readonly=True)
        _, udata = mail.search(None, "UNSEEN")
        unread_ids = udata[0].split() if udata[0] else []
        _, adata = mail.search(None, "ALL")
        all_ids = adata[0].split() if adata[0] else []
        fetch_ids = all_ids[-GMAIL_MAX_MSGS:] if len(all_ids) >= GMAIL_MAX_MSGS else all_ids
        fetch_ids = fetch_ids[::-1]
        unread_set = set(unread_ids)
        msgs = []
        for uid in fetch_ids:
            _, data = mail.fetch(uid, "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if not data or not data[0]:
                continue
            raw_header = data[0][1] if isinstance(data[0], tuple) else b""
            msg = email.message_from_bytes(raw_header)
            subject  = _decode_str(msg.get("Subject", "(no subject)"))
            sender   = _decode_str(msg.get("From", ""))
            date_str = msg.get("Date", "")
            try:
                parsed = email.utils.parsedate_to_datetime(date_str)
                ts_iso = parsed.astimezone(timezone.utc).isoformat()
            except Exception:
                ts_iso = ""
            msgs.append({
                "uid":     uid.decode(),
                "subject": subject[:80],
                "from":    sender[:60],
                "ts":      ts_iso,
                "unread":  uid in unread_set,
            })
        mail.logout()
        return {"configured": True, "unread": len(unread_ids), "emails": msgs}
    except Exception as e:
        return {"configured": True, "error": str(e), "unread": 0, "emails": []}


def _strip_html(html_str):
    class _S(_HTMLParser):
        def __init__(self):
            super().__init__(); self.parts = []
        def handle_data(self, d): self.parts.append(d)
        def handle_starttag(self, tag, attrs):
            if tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4"):
                self.parts.append("\n")
    s = _S(); s.feed(html_str)
    text = "".join(s.parts)
    text = _re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _fetch_gmail_body(uid):
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return {"error": "not configured"}
    if not uid.isdigit():
        return {"error": "invalid uid"}
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=15)
        mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        mail.select("INBOX", readonly=True)
        _, data = mail.fetch(uid.encode(), "(RFC822)")
        if not data or not data[0]:
            mail.logout()
            return {"error": "message not found"}
        raw = data[0][1]
        msg = email.message_from_bytes(raw)
        subject  = _decode_str(msg.get("Subject", ""))
        sender   = _decode_str(msg.get("From", ""))
        date_str = msg.get("Date", "")
        try:
            parsed = email.utils.parsedate_to_datetime(date_str)
            ts_iso = parsed.astimezone(timezone.utc).isoformat()
        except Exception:
            ts_iso = ""
        body = ""
        html_fallback = ""
        if msg.is_multipart():
            for part in msg.walk():
                ct  = part.get_content_type()
                cd  = str(part.get("Content-Disposition", ""))
                if "attachment" in cd:
                    continue
                if ct == "text/plain" and not body:
                    raw_b = part.get_payload(decode=True) or b""
                    body = raw_b.decode(part.get_content_charset() or "utf-8", errors="replace")
                elif ct == "text/html" and not html_fallback:
                    raw_b = part.get_payload(decode=True) or b""
                    html_fallback = raw_b.decode(part.get_content_charset() or "utf-8", errors="replace")
        else:
            raw_b = msg.get_payload(decode=True) or b""
            text  = raw_b.decode(msg.get_content_charset() or "utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                html_fallback = text
            else:
                body = text
        if not body and html_fallback:
            body = _strip_html(html_fallback)
        mail.logout()
        return {"subject": subject, "from": sender, "ts": ts_iso, "body": body[:6000]}
    except Exception as e:
        return {"error": str(e)}


def _delete_gmail(uid):
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return {"error": "not configured"}
    if not uid.isdigit():
        return {"error": "invalid uid"}
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=15)
        mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        mail.select("INBOX")
        mail.copy(uid.encode(), "[Gmail]/Trash")
        mail.store(uid.encode(), "+FLAGS", "\\Deleted")
        mail.expunge()
        mail.logout()
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


# ── Background scheduler ──────────────────────────────────────────────────────

def _build_next_run(start: int, interval: int) -> str:
    """Return ISO timestamp of the next scheduled run for a given start hour + interval."""
    now_et = datetime.now(_ET)
    today  = now_et.date()
    hours  = sorted({(start + i * interval) % 24 for i in range(24 // interval)})
    for h in hours:
        candidate = datetime(today.year, today.month, today.day, h, 0, 0, tzinfo=_ET)
        if candidate > now_et:
            return candidate.isoformat()
    tomorrow = today + timedelta(days=1)
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, hours[0], 0, 0, tzinfo=_ET).isoformat()


def _build_schedule(start: int, interval: int) -> frozenset:
    """Return the set of ET hours an agent should run.
    Runs every `interval` hours starting at `start`.
    Sleep window (2am → start) is excluded automatically."""
    sleep_window = set(range(2, start)) if start > 2 else set()
    hours = {(start + i * interval) % 24 for i in range(24 // interval)}
    return frozenset(hours - sleep_window)


_AGENT_SCHEDULES = {
    "trend_setter":    _build_schedule(TREND_SETTER_START_HOUR,  TREND_SETTER_INTERVAL_HRS),
    "system_analyzer": _build_schedule(SYS_ANALYZER_START_HOUR,  SYS_ANALYZER_INTERVAL_HRS),
    "dev_scout":       _build_schedule(DEV_SCOUT_START_HOUR,     DEV_SCOUT_INTERVAL_HRS),
}

_scheduler_lock = threading.Lock()
_hours_ran: set = set()   # tracks (agent, et_date, et_hour) already fired


def _scheduler_loop():
    while True:
        now_et  = datetime.now(_ET)
        et_hour = now_et.hour
        et_date = now_et.date()

        with _scheduler_lock:
            global _hours_ran
            _hours_ran = {k for k in _hours_ran if k[1] == et_date}  # prune old dates

            for agent, schedule in _AGENT_SCHEDULES.items():
                key = (agent, et_date, et_hour)
                if et_hour in schedule and key not in _hours_ran:
                    _hours_ran.add(key)
                    with _running_agents_lock:
                        already = agent in _running_agents
                        if not already:
                            _running_agents.add(agent)
                    if not already:
                        threading.Thread(target=_run_agent_guarded, args=(agent,), daemon=True).start()

        time.sleep(60)


def _run_agent(name: str):
    try:
        if name == "trend_setter":
            from agents.trend_setter import run
        elif name == "system_analyzer":
            from agents.system_analyzer import run
        elif name == "dev_scout":
            from agents.dev_scout import run
        else:
            return
        print(f"[scheduler] running {name}…")
        run()
        print(f"[scheduler] {name} complete")
    except Exception as e:
        print(f"[scheduler] {name} error: {e}")


_running_agents: set = set()
_running_agents_lock = threading.Lock()


def _run_agent_guarded(name: str):
    """Run agent and clear the running-lock when done."""
    try:
        _run_agent(name)
    finally:
        with _running_agents_lock:
            _running_agents.discard(name)


# Start scheduler thread
_sched_thread = threading.Thread(target=_scheduler_loop, daemon=True)
_sched_thread.start()


# ── API routes ────────────────────────────────────────────────────────────────

@app.route("/api/status")
@login_required
def api_status():
    return jsonify(_read_json(STATUS_FILE, {"state": "offline", "ts": ""}))


@app.route("/api/conversation")
@login_required
def api_conversation():
    return jsonify(_read_json(LOG_FILE, []))


@app.route("/api/stats")
@login_required
def api_stats():
    return jsonify(_system_stats())


@app.route("/api/chat", methods=["POST"])
@login_required
def api_chat():
    if not _brain_ok:
        return jsonify({"error": "Brain offline — install anthropic: pip install anthropic"}), 503
    data = request.get_json(silent=True) or {}
    msg  = (data.get("message") or "").strip()
    if not msg:
        return jsonify({"error": "empty message"}), 400
    with _brain_lock:
        _write_status("thinking")
        try:
            reply = _brain.process(msg)
        except Exception as e:
            _write_status("idle")
            return jsonify({"error": str(e)}), 500
        _log_exchange(msg, reply)
        _write_status("idle")
    return jsonify({"reply": reply})


@app.route("/api/chat/stream", methods=["POST"])
@login_required
def api_chat_stream():
    """SSE endpoint — streams Claude's reply token by token."""
    if not _brain_ok:
        return jsonify({"error": "Brain offline"}), 503
    data  = request.get_json(silent=True) or {}
    msg   = (data.get("message") or "").strip()
    image = data.get("image") or None
    if not msg and not image:
        return jsonify({"error": "empty message"}), 400

    def generate():
        full_reply = []
        with _brain_lock:
            _write_status("thinking")
            try:
                for chunk in _brain.process_stream(msg, image_b64=image):
                    full_reply.append(chunk)
                    yield f"data: {json.dumps({'chunk': chunk})}\n\n"
                _log_exchange(msg, "".join(full_reply))
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            finally:
                _write_status("idle")
        yield "data: {\"done\": true}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _write_status(state: str):
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(STATUS_FILE, "w") as f:
            json.dump({"state": state, "ts": datetime.now(timezone.utc).isoformat()}, f)
    except Exception:
        pass


def _log_exchange(user_text: str, dewd_text: str):
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        try:
            with open(LOG_FILE) as f:
                entries = json.load(f)
        except Exception:
            entries = []
        entries.append({"ts": datetime.now(timezone.utc).isoformat(), "user": user_text, "dewd": dewd_text})
        entries = entries[-MAX_LOG_ENTRIES:]
        with open(LOG_FILE, "w") as f:
            json.dump(entries, f)
    except Exception:
        pass


@app.route("/api/gmail")
@login_required
def api_gmail():
    return jsonify(_fetch_gmail())


@app.route("/api/gmail/<uid>")
@login_required
def api_gmail_body(uid):
    return jsonify(_fetch_gmail_body(uid))


@app.route("/api/gmail/<uid>/delete", methods=["POST"])
@login_required
def api_gmail_delete(uid):
    return jsonify(_delete_gmail(uid))


@app.route("/api/stream")
@login_required
def api_stream():
    def generate():
        last = None
        while True:
            data = _read_json(STATUS_FILE, {"state": "offline", "ts": ""})
            if data != last:
                last = data
                yield f"data: {json.dumps(data)}\n\n"
            time.sleep(0.8)
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Agent API routes ──────────────────────────────────────────────────────────

_KNOWN_AGENTS = ("trend_setter", "system_analyzer", "dev_scout")


def _atomic_write_json(path: str, data: dict):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


@app.route("/api/agents/<name>")
@login_required
def api_agent_result(name):
    if name not in _KNOWN_AGENTS:
        return jsonify({"error": "unknown agent"}), 404
    path = os.path.join(AGENTS_DIR, f"{name}.json")
    data = _read_json(path, {"status": "never_run", "report": None})
    # _running_agents is the authoritative live state — override JSON if mismatched
    with _running_agents_lock:
        if name in _running_agents:
            data["status"] = "running"
    return jsonify(data)


@app.route("/api/weather")
@login_required
def api_weather():
    """3-day forecast via wttr.in — no API key required."""
    loc = request.args.get("location", WEATHER_LOCATION)
    try:
        r = requests.get(
            f"https://wttr.in/{requests.utils.quote(loc)}",
            params={"format": "j1"},
            timeout=8,
        )
        r.raise_for_status()
        raw = r.json()
        current = raw["current_condition"][0]
        days = []
        for d in raw.get("weather", [])[:3]:
            desc = d.get("hourly", [{}])[4].get("weatherDesc", [{}])[0].get("value", "")
            days.append({
                "date":      d.get("date", ""),
                "max_f":     d.get("maxtempF", ""),
                "min_f":     d.get("mintempF", ""),
                "desc":      desc,
            })
        return jsonify({
            "location":   loc,
            "temp_f":     current.get("temp_F", ""),
            "feels_f":    current.get("FeelsLikeF", ""),
            "desc":       current.get("weatherDesc", [{}])[0].get("value", ""),
            "humidity":   current.get("humidity", ""),
            "wind_mph":   current.get("windspeedMiles", ""),
            "forecast":   days,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/agents/system_analyzer/history")
@login_required
def api_sysana_history():
    """Return rolling hardware history for sparkline charts."""
    hist_file = os.path.join(AGENTS_DIR, "system_analyzer_history.json")
    return jsonify(_read_json(hist_file, []))


@app.route("/api/agents/<name>/run", methods=["POST"])
@login_required
def api_agent_run(name):
    if name not in _KNOWN_AGENTS:
        return jsonify({"error": "unknown agent"}), 404
    with _running_agents_lock:
        if name in _running_agents:
            return jsonify({"ok": False, "message": f"{name} already running"}), 409
        _running_agents.add(name)
    threading.Thread(target=_run_agent_guarded, args=(name,), daemon=True).start()
    return jsonify({"ok": True, "message": f"{name} started"})


@app.route("/api/agents/<name>/run/stream", methods=["POST"])
@login_required
def api_agent_run_stream(name):
    """SSE endpoint — streams agent analysis live as it's generated."""
    if name not in _KNOWN_AGENTS:
        return jsonify({"error": "unknown agent"}), 404
    with _running_agents_lock:
        if name in _running_agents:
            return jsonify({"ok": False, "message": f"{name} already running"}), 409
        _running_agents.add(name)

    def generate():
        output_file = None
        try:
            if name == "trend_setter":
                from agents.trend_setter import (
                    gather_ai_posts, gather_world_signals, gather_rss_signals,
                    analyze_stream, _build_top_posts,
                    _write_status, _next_scheduled_run, OUTPUT_FILE,
                )
                output_file = OUTPUT_FILE
                _write_status("running")
                yield f"data: {json.dumps({'msg': 'Fetching Reddit posts…'})}\n\n"
                ai_posts    = gather_ai_posts()
                world_posts = gather_world_signals()
                yield f"data: {json.dumps({'msg': 'Fetching AI RSS feeds…'})}\n\n"
                rss_items   = gather_rss_signals()
                total = len(ai_posts) + len(world_posts)
                yield f"data: {json.dumps({'msg': f'Analyzing {total} posts + RSS…'})}\n\n"
                full = ""
                for chunk in analyze_stream(ai_posts, world_posts, rss_items):
                    full += chunk
                    yield f"data: {json.dumps({'chunk': chunk})}\n\n"
                top = _build_top_posts(ai_posts, 5) + _build_top_posts(world_posts, 3)
                result = {
                    "status":     "ok",
                    "ran_at":     datetime.now(timezone.utc).isoformat(),
                    "post_count": total,
                    "report":     full,
                    "top_posts":  top,
                    "next_run":   _next_scheduled_run(),
                }
                _atomic_write_json(OUTPUT_FILE, result)

            elif name == "system_analyzer":
                from agents.system_analyzer import (
                    collect_all, analyze_stream, _append_history,
                    _check_alerts, _write_status, OUTPUT_FILE,
                )
                output_file = OUTPUT_FILE
                _write_status("running")
                yield f"data: {json.dumps({'msg': 'Collecting system telemetry…'})}\n\n"
                raw = collect_all()
                _append_history(raw["hardware"])
                yield f"data: {json.dumps({'msg': 'Analyzing with Haiku…'})}\n\n"
                full = ""
                for chunk in analyze_stream(raw):
                    full += chunk
                    yield f"data: {json.dumps({'chunk': chunk})}\n\n"
                _check_alerts(raw["hardware"], full)
                result = {
                    "status":       "ok",
                    "ran_at":       raw["collected_at"],
                    "report":       full,
                    "next_run":     _build_next_run(SYS_ANALYZER_START_HOUR, SYS_ANALYZER_INTERVAL_HRS),
                    "raw_snapshot": {
                        "hardware":           raw["hardware"],
                        "active_connections": raw["active_connections"][:20],
                        "failed_logins":      raw["failed_logins"],
                        "wifi":               raw["wifi"],
                    },
                }
                _atomic_write_json(OUTPUT_FILE, result)

            elif name == "dev_scout":
                from agents.dev_scout import (
                    gather, analyze_stream, _write_status, _next_run, OUTPUT_FILE,
                )
                try:
                    from notify import send_alert as _ntfy
                except Exception:
                    def _ntfy(*a, **kw): return False
                output_file = OUTPUT_FILE
                _write_status("running")
                yield f"data: {json.dumps({'msg': 'Scanning GitHub · HN · PyPI · RSS…'})}\n\n"
                signals = gather()
                yield f"data: {json.dumps({'msg': 'Analyzing with Sonnet…'})}\n\n"
                full = ""
                for chunk in analyze_stream(signals):
                    full += chunk
                    yield f"data: {json.dumps({'chunk': chunk})}\n\n"
                if "CRITICAL" in full:
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
                    "report":    full,
                    "top_finds": top_finds,
                    "packages":  signals["packages"],
                    "next_run":  _next_run(),
                }
                _atomic_write_json(OUTPUT_FILE, result)

            yield f"data: {json.dumps({'done': True})}\n\n"

        except Exception as e:
            # Write error status to file so dashboard shows "error" not stuck "running"
            if output_file:
                try:
                    existing = _read_json(output_file, {})
                    existing.update({
                        "status":  "error",
                        "error":   str(e),
                        "ran_at":  datetime.now(timezone.utc).isoformat(),
                    })
                    _atomic_write_json(output_file, existing)
                except Exception:
                    pass
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            with _running_agents_lock:
                _running_agents.discard(name)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Dashboard ─────────────────────────────────────────────────────────────────

_DASHBOARD_TMPL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard_template.html")
DASHBOARD_HTML = open(_DASHBOARD_TMPL, encoding="utf-8").read()


@app.route("/favicon.ico")
def favicon():
    # Inline 1×1 transparent ICO — stops 404 spam in logs
    ICO = (b"\x00\x00\x01\x00\x01\x00\x01\x01\x00\x00\x01\x00\x18\x00"
           b"\x30\x00\x00\x00\x16\x00\x00\x00\x28\x00\x00\x00\x01\x00"
           b"\x00\x00\x02\x00\x00\x00\x01\x00\x18\x00\x00\x00\x00\x00"
           b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
           b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00")
    from flask import make_response
    resp = make_response(ICO)
    resp.headers["Content-Type"] = "image/x-icon"
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


@app.route("/")
@login_required
def dashboard():
    return render_template_string(DASHBOARD_HTML)


if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(AGENTS_DIR, exist_ok=True)
    _write_status("idle")
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w") as f:
            json.dump([], f)

    print("━" * 54)
    print("  DEWD Mission Control  →  http://0.0.0.0:8080")
    print(f"  Brain: {'online' if _brain_ok else 'OFFLINE (check anthropic key)'}")
    print("  Trend Setter:     6am 10am 2pm 6pm 10pm ET  (sleep 2am–6am)")
    print("  System Analyzer:  6am 10am 2pm 6pm 10pm ET  (sleep 2am–6am)")
    print("  Dev Scout:        8am 12pm 4pm 8pm midnight ET  (sleep 2am–8am)")
    print("━" * 54)
    app.run(host="0.0.0.0", port=8080, threaded=True)
