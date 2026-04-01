# DEWD — Dashboard & AI Assistant

**DEWD** (pronounced like "dude") is a personal AI dashboard running on a Raspberry Pi 5.
It combines a Claude-powered conversational brain with a live web dashboard (SHIN-DEWD) and a suite of background intelligence agents.

Access it from anywhere via Tailscale or Raspberry Pi Connect on your phone or desktop.

---

## What It Does

**AI Chat** — Talk to Claude via the browser. Responses stream in real time. Conversation history persists across restarts.

**Live System Monitor** — CPU, RAM, disk, temperature, uptime, and network I/O — all updated live with sparkline history charts.

**Gmail Inbox** — Read and delete emails directly from the dashboard. No browser needed.

**Weather** — Current conditions and 3-day forecast for your location.

**Background Agents** — Three autonomous agents run on schedule and surface findings in the Intel panel:

| Agent | What It Does | Schedule |
|---|---|---|
| **Trend Setter** | Scans Reddit (r/ClaudeAI, r/LocalLLaMA, r/LLMDevs, r/PromptEngineering, r/MachineLearning, etc.), Hugging Face Blog, and arXiv cs.AI+cs.CL for trending AI projects and world signals | 5× daily (ET) |
| **System Analyzer** | Full Pi health check — hardware stats, network connections, open ports, failed logins, anomaly detection, security posture (UFW + fail2ban) | Every 4 hrs (ET) |
| **Dev Scout** | Scans GitHub, HackerNews, PyPI, and tech RSS (including Real Python + PyPI Updates) for upgrades and improvements relevant to the DEWD stack | Every 4 hrs (ET) |

---

## Stack

- **Hardware** — Raspberry Pi 5 (16GB)
- **Brain** — [Claude API](https://www.anthropic.com) (`claude-sonnet-4-6`) with prompt caching
- **Dashboard** — Flask + vanilla JS, served on port 8080
- **Remote access** — [Tailscale](https://tailscale.com) + [Raspberry Pi Connect](https://connect.raspberrypi.com) (screen share)
- **Push alerts** — [ntfy.sh](https://ntfy.sh) (optional)

---

## Setup

### 1. Clone and configure

```bash
git clone https://github.com/YOUR_USERNAME/dewd.git
cd dewd
cp .env.example .env
nano .env   # add your Anthropic API key and optional credentials
```

### 2. Create a virtual environment and install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Run

```bash
python3 dewd_web.py
```

Dashboard is live at `http://<pi-ip>:8080`

---

## Configuration

All secrets live in `.env` (never committed). See [`.env.example`](.env.example) for all options.

Key settings in [`config.py`](config.py):

| Setting | Default | Description |
|---|---|---|
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | Claude model used for chat |
| `MAX_HISTORY_TURNS` | `25` | Conversation turns kept in context |
| `GMAIL_MAX_MSGS` | `8` | Emails shown in dashboard inbox |
| `TREND_SETTER_START_HOUR` | `6` | First run of day ET, then every 4 hrs |
| `SYS_ANALYZER_START_HOUR` | `6` | First run of day ET, then every 4 hrs |
| `DEV_SCOUT_START_HOUR` | `8` | First run of day ET, then every 4 hrs |

---

## Dashboard Layouts & Themes

**Layouts**
- `BRIDGE` — Two-panel side-by-side (desktop)
- `PANELS` — Single panel with tab navigation (mobile)

Auto-detected on load based on screen width. Can be manually overridden.

**Themes** (dropdown order)
- `ARCADE` — TROPICAL synthwave palette (default)
- `QUANTUM` — Deep blue/cyan
- `NEON` — Pink/magenta
- `SECTOR` — KND-inspired: mission red, treehouse green, ops yellow

---

## Project Structure

```
dewd/
├── dewd_web.py          # Flask server, API routes, background scheduler
├── brain.py             # Claude API client, conversation history, tool loop
├── tools.py             # Tool definitions and safe execution sandbox
├── config.py            # All configuration (reads from .env)
├── notify.py            # ntfy.sh push alert helper
├── dashboard_template.html  # Dashboard frontend (single-file, no build step)
├── setup.sh             # One-time setup script
├── requirements.txt
├── .env.example
└── agents/
    ├── trend_setter.py     # Reddit + Hugging Face Blog + arXiv RSS trend scanner
    ├── system_analyzer.py  # Pi health + security posture monitor
    └── dev_scout.py        # GitHub/HN/PyPI/RSS scanner
```

---

## Security

- All secrets loaded from `.env` (owner-read only, gitignored)
- **SSH disabled** — not in use; access is via Tailscale VPN or Pi Connect screen share only
- **UFW firewall** — deny all incoming by default; port 8080 restricted to Tailscale subnet (100.64.0.0/10) only
- **fail2ban** — installed for defense-in-depth
- **unattended-upgrades** — automatic security patching enabled
- **rpcbind disabled** — port 111 removed
- `run_command` tool has a hardened blocklist — kills, shutdown, sudo, reverse shells, env dumping, and encoded payloads are all blocked
- Subprocess environment strips secrets before execution
- `read_file` tool blocks access to credential files and SSH keys
- InfluxDB bound to `127.0.0.1` only
- Dashboard has password auth on desktop; mobile bypassed (Tailscale already authenticates)
- System Analyzer runs `ss` with sudo via a locked sudoers entry (`/etc/sudoers.d/dewd-analyzer`) for full process attribution on connections

---

## Notes

- Conversation history is in-memory and reloads from `data/conversation.json` on restart (last 25 turns)
- The `data/` directory is gitignored — it holds your local conversation log and agent output
- Intel tab caches last agent report per agent — no blank screens on tab switch
- Swipe left/right on the Intel panel to switch between Dev Scout, Trend Setter, and Sys-Ana
- Schedules use Eastern Time (EST/EDT handled automatically via `zoneinfo`)
- Sleep window: no agent runs between 2am and the configured start hour
- Tested on Raspberry Pi 5 (16GB) running Raspberry Pi OS (64-bit)

---

*Built for personal use. Use responsibly.*
