#!/usr/bin/env python3
"""
DEWD Mission Control — served at http://<pi-ip>:8080
Text-only dashboard: chat via browser → brain.py → Claude API.
Runs background scheduler for Daymark, Frontier, and Smith agents.
Background stats loop records hardware history and fires threshold alerts.
"""
import hmac
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
import queue as _queue
from email.header import decode_header as _decode_header
from html.parser import HTMLParser as _HTMLParser
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
from functools import wraps
from flask import Flask, Response, jsonify, render_template_string, request, session, redirect
from agents.common import atomic_write as _atomic_write

from config import (
    DATA_DIR, STATUS_FILE, LOG_FILE,
    GMAIL_ADDRESS, GMAIL_APP_PASSWORD, GMAIL_MAX_MSGS,
    MAX_LOG_ENTRIES,
    DAYMARK_HOURS, FRONTIER_HOURS,
    AGENTS_DIR, CALENDAR_FILE,
    WEATHER_LOCATION,
    SECRET_KEY, DASHBOARD_PASSWORD,
    NTFY_URL, NTFY_TOPIC,
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

_login_attempts: dict = {}   # ip -> [timestamp, ...]
_LOGIN_MAX    = 5            # max attempts
_LOGIN_WINDOW = 900          # 15-minute window in seconds

def _login_allowed(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _login_attempts.get(ip, []) if now - t < _LOGIN_WINDOW]
    if attempts:
        _login_attempts[ip] = attempts
    elif ip in _login_attempts:
        del _login_attempts[ip]
    return len(attempts) < _LOGIN_MAX

def _login_record(ip: str):
    _login_attempts.setdefault(ip, []).append(time.time())

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
    .locked{font-size:.65rem;letter-spacing:.1em;color:#fb923c;margin-top:16px}
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
      {% if locked %}<div class="locked">TOO MANY ATTEMPTS — WAIT 15 MIN</div>
      {% elif error %}<div class="error">INVALID CODE — TRY AGAIN</div>{% endif %}
    </form>
  </div>
</body>
</html>"""


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("authenticated"):
            return f(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"error": "unauthorized"}), 401
        return redirect("/login")
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    if not DASHBOARD_PASSWORD:
        # No password configured — block all access rather than allow blank auth
        return render_template_string(_LOGIN_HTML, error=False, locked=True), 503
    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        if not _login_allowed(ip):
            return render_template_string(_LOGIN_HTML, error=True, locked=True)
        if hmac.compare_digest(request.form.get("password", ""), DASHBOARD_PASSWORD):
            session["authenticated"] = True
            _login_attempts.pop(ip, None)  # clear failed attempts on success
            return redirect("/")
        _login_record(ip)
        return render_template_string(_LOGIN_HTML, error=True, locked=False)
    return render_template_string(_LOGIN_HTML, error=False, locked=False)


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
        try:
            stats["temp_c"] = float(stats["temp"].replace("'C", "").replace("°C", ""))
        except Exception:
            stats["temp_c"] = None
    except Exception:
        stats["temp"] = "—"
        stats["temp_c"] = None
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
        delta = total1 - total0
        stats["cpu_pct"] = round(100.0 * (1 - (idle1 - idle0) / delta), 1) if delta > 0 else 0
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
    mail = None
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
        return {"configured": True, "unread": len(unread_ids), "emails": msgs}
    except Exception as e:
        return {"configured": True, "error": str(e), "unread": 0, "emails": []}
    finally:
        if mail:
            try: mail.logout()
            except Exception: pass


def _strip_html(html_str):
    class _S(_HTMLParser):
        _SKIP_TAGS = {"style", "script", "head"}
        _BLOCK_OPEN = {"p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "br", "hr", "blockquote", "pre"}
        _BLOCK_CLOSE = {"p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"}
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []
            self._depth = 0  # nesting depth inside skip tags
        def handle_data(self, d):
            if self._depth == 0:
                self.parts.append(d)
        def handle_starttag(self, tag, attrs):
            if tag in self._SKIP_TAGS:
                self._depth += 1
                return
            if self._depth == 0 and tag in self._BLOCK_OPEN:
                self.parts.append("\n")
        def handle_endtag(self, tag):
            if tag in self._SKIP_TAGS:
                self._depth = max(0, self._depth - 1)
                return
            if self._depth == 0 and tag in self._BLOCK_CLOSE:
                self.parts.append("\n")
    s = _S()
    s.feed(html_str)
    text = "".join(s.parts)
    text = _re.sub(r"[ \t]+", " ", text)           # collapse inline whitespace
    text = _re.sub(r" *\n *", "\n", text)           # trim spaces around newlines
    text = _re.sub(r"\n{3,}", "\n\n", text)         # max two consecutive blank lines
    return text.strip()


def _fetch_gmail_body(uid):
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return {"error": "not configured"}
    if not (uid.isascii() and uid.isdigit()):
        return {"error": "invalid uid"}
    mail = None
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
        return {"subject": subject, "from": sender, "ts": ts_iso, "body": body[:6000]}
    except Exception as e:
        return {"error": str(e)}
    finally:
        if mail:
            try: mail.logout()
            except Exception: pass


def _delete_gmail(uid):
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return {"error": "not configured"}
    if not (uid.isascii() and uid.isdigit()):
        return {"error": "invalid uid"}
    mail = None
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=15)
        mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        mail.select("INBOX")
        mail.copy(uid.encode(), "[Gmail]/Trash")
        mail.store(uid.encode(), "+FLAGS", "\\Deleted")
        mail.expunge()
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}
    finally:
        if mail:
            try: mail.logout()
            except Exception: pass


# ── Background scheduler ──────────────────────────────────────────────────────

# Schedules: Daymark 3x daily, Frontier 2x daily.
# Frontier always triggers Smith on completion.
# Smith fires the morning brief if it runs within SMITH_BRIEF_WINDOW.

_AGENT_SCHEDULES = {
    "daymark":  frozenset(DAYMARK_HOURS),
    "frontier": frozenset(FRONTIER_HOURS),
    # smith has no fixed schedule — always triggered by frontier
}

_scheduler_lock  = threading.Lock()
_hours_ran: set  = set()   # tracks (agent, et_date, et_hour) already fired

_running_agents      : set = set()
_running_agents_lock       = threading.Lock()


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
                    with _running_agents_lock:
                        already = agent in _running_agents
                        if not already:
                            _running_agents.add(agent)
                            _hours_ran.add(key)  # only mark ran if we actually start it
                    if not already:
                        threading.Thread(
                            target=_run_agent_guarded, args=(agent,), daemon=True
                        ).start()

        time.sleep(60)


def _run_agent(name: str):
    try:
        if name == "daymark":
            from agents.daymark import run
        elif name == "frontier":
            from agents.frontier import run
        elif name == "smith":
            from agents.smith import run
        else:
            return
        print(f"[scheduler] running {name}…")
        run()
        print(f"[scheduler] {name} complete")
    except Exception as e:
        print(f"[scheduler] {name} error: {e}")


def _run_agent_guarded(name: str):
    """Run agent and clear the running-lock when done.
    Frontier always triggers Smith on completion."""
    try:
        _run_agent(name)
        if name == "frontier":
            with _running_agents_lock:
                already = "smith" in _running_agents
                if not already:
                    _running_agents.add("smith")
            if not already:
                print("[scheduler] frontier complete — triggering smith")
                threading.Thread(
                    target=_run_agent_guarded, args=("smith",), daemon=True
                ).start()
    finally:
        with _running_agents_lock:
            _running_agents.discard(name)


# On startup, reset any agent left in "running" state by a previously killed process.
def _reset_stale_running():
    for name in ("daymark", "frontier", "smith"):
        path = os.path.join(AGENTS_DIR, f"{name}.json")
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                d = json.load(f)
            if d.get("status") == "running":
                d["status"] = "error"
                d.setdefault("report", "Agent interrupted — process was killed mid-run.")
                _atomic_write(path, d)
                print(f"[startup] cleared stale 'running' for {name}")
        except Exception:
            pass


_reset_stale_running()

# Start scheduler thread
_sched_thread = threading.Thread(target=_scheduler_loop, daemon=True)
_sched_thread.start()


# ── Background stats loop ─────────────────────────────────────────────────────
# Records hardware stats every 5 min and fires ntfy alerts on threshold breach.

_STATS_HISTORY_FILE  = os.path.join(AGENTS_DIR, "stats_history.json")
_STATS_INTERVAL      = 300   # seconds between recordings
_STATS_MAX_ENTRIES   = 288   # 24h at 5-min intervals
_ALERT_TEMP_C        = 80.0
_ALERT_RAM_PCT       = 90.0
_last_alert_ts: dict = {}    # alert_key -> last fired timestamp


def _maybe_alert(key: str, title: str, body: str, cooldown_s: int = 3600):
    """Send ntfy alert with cooldown to avoid repeat spam."""
    if not NTFY_TOPIC:
        return
    now = time.time()
    if now - _last_alert_ts.get(key, 0) < cooldown_s:
        return
    _last_alert_ts[key] = now
    try:
        requests.post(
            f"{NTFY_URL}/{NTFY_TOPIC}",
            data=body.encode(),
            headers={"Title": title, "Priority": "high"},
            timeout=8,
        )
    except Exception as e:
        print(f"[stats] ntfy alert failed: {e}")


def _stats_loop():
    while True:
        try:
            stats = _system_stats()
            entry = {
                "ts":      datetime.now(timezone.utc).isoformat(),
                "cpu_pct": stats.get("cpu_pct"),
                "ram_pct": stats.get("ram_pct"),
                "temp_c":  stats.get("temp_c"),
                "disk_pct": stats.get("disk_pct"),
            }

            # Append to rolling history
            try:
                with open(_STATS_HISTORY_FILE) as f:
                    history = json.load(f)
                if not isinstance(history, list):
                    history = []
            except Exception:
                history = []
            history.append(entry)
            if len(history) > _STATS_MAX_ENTRIES:
                history = history[-_STATS_MAX_ENTRIES:]
            _atomic_write(_STATS_HISTORY_FILE, history)

            # Threshold alerts
            temp_c = stats.get("temp_c") or 0.0
            if isinstance(temp_c, (int, float)) and temp_c >= _ALERT_TEMP_C:
                _maybe_alert(
                    "temp_high",
                    f"DEWD — Pi temp {temp_c:.1f}°C",
                    f"Pi temperature is {temp_c:.1f}°C — above {_ALERT_TEMP_C}°C threshold.",
                )

            ram_pct = stats.get("ram_pct") or 0.0
            if isinstance(ram_pct, (int, float)) and ram_pct >= _ALERT_RAM_PCT:
                _maybe_alert(
                    "ram_high",
                    f"DEWD — RAM at {ram_pct:.0f}%",
                    f"RAM usage is {ram_pct:.0f}% — above {_ALERT_RAM_PCT}% threshold.",
                )

        except Exception as e:
            print(f"[stats] loop error: {e}")

        time.sleep(_STATS_INTERVAL)


_stats_thread = threading.Thread(target=_stats_loop, daemon=True)
_stats_thread.start()


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
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"state": state, "ts": datetime.now(timezone.utc).isoformat()}, f)
        os.replace(tmp, STATUS_FILE)
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
        tmp = LOG_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(entries, f)
        os.replace(tmp, LOG_FILE)
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


# ── Calendar ──────────────────────────────────────────────────────────────────

import uuid as _uuid

def _load_calendar() -> list:
    try:
        with open(CALENDAR_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def _save_calendar(events: list):
    os.makedirs(os.path.dirname(CALENDAR_FILE), exist_ok=True)
    tmp = CALENDAR_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(events, f, indent=2)
    os.replace(tmp, CALENDAR_FILE)

@app.route("/api/calendar")
@login_required
def api_calendar_get():
    return jsonify(_load_calendar())

@app.route("/api/calendar", methods=["POST"])
@login_required
def api_calendar_create():
    d = request.get_json(silent=True) or {}
    title = (d.get("title") or "").strip()
    start = (d.get("start") or "").strip()
    end   = (d.get("end")   or "").strip()
    if not title or not start:
        return jsonify({"error": "title and start required"}), 400
    event = {
        "uid":         str(_uuid.uuid4()),
        "title":       title[:120],
        "start":       start,
        "end":         end or start,
        "description": (d.get("description") or "")[:500],
        "all_day":     bool(d.get("all_day", False)),
        "color":       (d.get("color") or ""),
    }
    events = _load_calendar()
    events.append(event)
    _save_calendar(events)
    return jsonify(event), 201

@app.route("/api/calendar/<uid>", methods=["PUT"])
@login_required
def api_calendar_update(uid):
    d = request.get_json(silent=True) or {}
    events = _load_calendar()
    for i, ev in enumerate(events):
        if ev.get("uid") == uid:
            if "title"       in d: events[i]["title"]       = (d["title"]       or "").strip()[:120]
            if "start"       in d: events[i]["start"]       = (d["start"]       or "").strip()
            if "end"         in d: events[i]["end"]         = (d["end"]         or "").strip()
            if "description" in d: events[i]["description"] = (d["description"] or "")[:500]
            if "all_day"     in d: events[i]["all_day"]     = bool(d["all_day"])
            if "color"       in d: events[i]["color"]       = (d["color"]       or "")
            _save_calendar(events)
            return jsonify(events[i])
    return jsonify({"error": "not found"}), 404

@app.route("/api/calendar/<uid>", methods=["DELETE"])
@login_required
def api_calendar_delete(uid):
    events = _load_calendar()
    new_events = [ev for ev in events if ev.get("uid") != uid]
    if len(new_events) == len(events):
        return jsonify({"error": "not found"}), 404
    _save_calendar(new_events)
    return jsonify({"ok": True})


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

_KNOWN_AGENTS = ("daymark", "frontier", "smith")


@app.route("/api/agents/<name>")
@login_required
def api_agent_result(name):
    if name not in _KNOWN_AGENTS:
        return jsonify({"error": "unknown agent"}), 404
    path = os.path.join(AGENTS_DIR, f"{name}.json")
    data = _read_json(path, {"status": "never_run", "report": None})
    # _running_agents is the authoritative live state — always wins over JSON
    with _running_agents_lock:
        if name in _running_agents:
            data["status"] = "running"
        elif data.get("status") == "running":
            # JSON says running but no active run in memory — process was killed mid-run
            data["status"] = "error"
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
            hourly = d.get("hourly") or []
            desc = (hourly[4] if len(hourly) > 4 else hourly[-1] if hourly else {}).get("weatherDesc", [{}])[0].get("value", "")
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


@app.route("/api/agents/stats/history")
@login_required
def api_stats_history():
    """Return rolling hardware history (5-min intervals, 24h window) for sparkline charts."""
    return jsonify(_read_json(_STATS_HISTORY_FILE, []))


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
    """SSE endpoint — streams agent events to the browser.

    The agent runs in its own daemon thread so closing the browser never
    kills the run mid-flight.  Events are pushed into a queue; the HTTP
    generator reads from the queue and forwards them.  When the browser
    disconnects the generator exits but the agent thread keeps running
    until it finishes and writes its JSON result file.
    """
    if name not in _KNOWN_AGENTS:
        return jsonify({"error": "unknown agent"}), 404
    with _running_agents_lock:
        if name in _running_agents:
            return jsonify({"ok": False, "message": f"{name} already running"}), 409
        _running_agents.add(name)

    q: _queue.Queue = _queue.Queue()
    _SENTINEL = object()

    def _agent_thread():
        try:
            if name == "daymark":
                from agents.daymark import stream_run
            elif name == "frontier":
                from agents.frontier import stream_run
            elif name == "smith":
                from agents.smith import stream_run
            else:
                q.put({"error": "unknown agent"})
                return
            for event in stream_run():
                q.put(event)
            q.put({"done": True})
            # Frontier always triggers Smith regardless of browser state
            if name == "frontier":
                with _running_agents_lock:
                    already = "smith" in _running_agents
                    if not already:
                        _running_agents.add("smith")
                if not already:
                    print("[stream] frontier complete — triggering smith")
                    threading.Thread(
                        target=_run_agent_guarded, args=("smith",), daemon=True
                    ).start()
        except Exception as e:
            q.put({"error": str(e)})
        finally:
            with _running_agents_lock:
                _running_agents.discard(name)
            q.put(_SENTINEL)

    threading.Thread(target=_agent_thread, daemon=True).start()

    def generate():
        while True:
            try:
                item = q.get(timeout=30)
            except _queue.Empty:
                # keepalive comment keeps the connection alive on slow agents
                yield ": keepalive\n\n"
                continue
            if item is _SENTINEL:
                break
            yield f"data: {json.dumps(item)}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Notes ─────────────────────────────────────────────────────────────────────

_NOTES_FILE = os.path.join(DATA_DIR, "notes.md")

@app.route("/api/notes", methods=["GET"])
@login_required
def api_notes_get():
    if os.path.exists(_NOTES_FILE):
        with open(_NOTES_FILE, encoding="utf-8") as f:
            return jsonify({"content": f.read()})
    return jsonify({"content": ""})

@app.route("/api/notes", methods=["POST"])
@login_required
def api_notes_post():
    data = request.get_json(silent=True) or {}
    content = data.get("content", "")[:500_000]  # 500 KB cap
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = _NOTES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, _NOTES_FILE)
    return jsonify({"ok": True})


# ── Dashboard ─────────────────────────────────────────────────────────────────

_DASHBOARD_TMPL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard_template.html")


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
    from config import CLAUDE_MODEL
    with open(_DASHBOARD_TMPL, encoding="utf-8") as _f:
        html = _f.read()
    return render_template_string(html, claude_model=CLAUDE_MODEL)


if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(AGENTS_DIR, exist_ok=True)
    _write_status("idle")
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w") as f:
            json.dump([], f)

    if SECRET_KEY == "change-me-in-env":
        print("⚠  WARNING: SECRET_KEY is default — set SECRET_KEY in .env")
    if not DASHBOARD_PASSWORD:
        print("⚠  WARNING: DASHBOARD_PASSWORD not set — dashboard will be inaccessible until set")

    print("━" * 54)
    print("  DEWD Mission Control  →  http://0.0.0.0:8080")
    print(f"  Brain: {'online' if _brain_ok else 'OFFLINE (check anthropic key)'}")
    print(f"  Daymark:          7am · 1pm · 7pm ET")
    print(f"  Frontier:         9am · 9pm ET  →  triggers Smith")
    print(f"  Smith:            standby — triggers after each Frontier run")
    print("━" * 54)
    app.run(host="0.0.0.0", port=8080, threaded=True)
