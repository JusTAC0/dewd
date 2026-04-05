# DEWD — Personal AI Dashboard

**DEWD** (pronounced like "dude") is a personal AI dashboard designed to run on a Raspberry Pi 5.
It combines a Claude-powered conversational brain with a live web dashboard and a suite of autonomous background intelligence agents.

Access it from anywhere via Tailscale or Raspberry Pi Connect.

---

## What It Does

**AI Chat** — Streaming conversation with Claude. Supports image uploads, tool use (shell, filesystem, web search), and persistent history across restarts.

**Live System Monitor** — CPU, RAM, disk, temperature, uptime, and network I/O — updated live with sparkline history charts.

**Gmail Inbox** — Read and delete emails directly from the dashboard.

**Calendar** — Personal event calendar with a month view and an animated radar/clock view. Events persist in `data/calendar.json`.

**Notes** — Persistent scratchpad, always one click away.

**Weather** — Current conditions and forecast for your configured location.

**Background Agents** — Three autonomous agents run on schedule and surface findings in the Intel panel:

| Agent | What It Does | Schedule |
|---|---|---|
| **Daymark** | World awareness — news, business, science, entertainment, sports, culture. Pulls from 30+ RSS feeds, Reddit, Wikipedia trending, and Google Trends. | 3× daily: 7am, 1pm, 7pm ET |
| **Frontier** | Tech scouting — scans GitHub, HackerNews, RSS tech feeds, Reddit, and PyPI for libraries, tools, and opportunities relevant to DEWD. | 2× daily: 9am, 9pm ET |
| **Smith** | System health — full Pi hardware audit, network connections, open ports, failed logins, anomaly detection, and security posture scoring. Triggered by Frontier. | After each Frontier run |

**Morning Chain** — The 7am Daymark run triggers Frontier, which triggers Smith, which composes a morning brief and delivers it via ntfy push notification.

---

## Stack

- **Hardware** — Raspberry Pi 5 (8GB or 16GB recommended)
- **Brain** — [Claude API](https://www.anthropic.com) — Haiku for chat, Sonnet for agents
- **Dashboard** — Flask + vanilla JS, single-file frontend, no build step
- **Remote access** — [Tailscale](https://tailscale.com) + [Raspberry Pi Connect](https://connect.raspberrypi.com)
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

### 2. Virtual environment and dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. (Optional) Configure known services for Smith

```bash
cp known_services.example.txt known_services.txt
nano known_services.txt   # document your system's expected services
```

Smith reads this file when auditing your network connections and processes so it does not flag your own services as anomalies. `known_services.txt` is gitignored.

### 4. Run

```bash
python3 dewd_web.py
```

Dashboard is live at `http://<pi-ip>:8080`

For persistent background operation, set up a systemd service or use `nohup`.

---

## Configuration

All secrets live in `.env` (gitignored). See [`.env.example`](.env.example) for all options.

Key settings in [`config.py`](config.py):

| Setting | Default | Description |
|---|---|---|
| `CLAUDE_MODEL` | `claude-haiku-4-5-20251001` | Claude model used for chat |
| `MAX_HISTORY_TURNS` | `10` | Conversation turns kept in context |
| `GMAIL_MAX_MSGS` | `8` | Emails shown in dashboard inbox |
| `DAYMARK_HOURS` | `(7, 13, 19)` | Daymark run times (ET) |
| `FRONTIER_HOURS` | `(9, 21)` | Frontier run times (ET) |
| `MORNING_CHAIN_HOUR` | `7` | Hour that triggers the morning chain |
| `SMITH_BRIEF_WINDOW` | `(8, 12)` | Hours in which Smith sends a morning brief |
| `SYS_HISTORY_MAX` | `48` | System stats history length (half-hour samples) |

---

## Dashboard

**Layouts**
- `BRIDGE` — Two-panel side-by-side (desktop)
- `PANELS` — Single panel with tab navigation (mobile/narrow)

Auto-detected on load based on screen width and pointer type. Can be manually overridden.

**Themes**
- `ARCADE` — Synthwave / tropical (default)
- `SECTOR` — Mission red, ops yellow, treehouse green

**Calendar Views**
- Month view — standard grid with event chips
- Radar view — animated clock-face radar showing events by time of day; pinch/scroll to zoom

---

## Project Structure

```
dewd/
├── dewd_web.py              # Flask server, API routes, background scheduler
├── brain.py                 # Claude chat brain — streaming, tool loop, history
├── tools.py                 # Tool definitions and sandboxed execution
├── config.py                # All configuration (reads from .env)
├── notify.py                # ntfy.sh push notification helper
├── dashboard_template.html  # Full dashboard UI (single file, no build step)
├── requirements.txt
├── setup.sh                 # One-time setup helper
├── .env.example             # Environment variable template
├── known_services.example.txt  # Template for Smith's known-services allowlist
└── agents/
    ├── common.py            # Shared utilities (atomic write, status helpers)
    ├── daymark.py           # World awareness agent
    ├── frontier.py          # Tech scouting agent
    └── smith.py             # System health agent
```

---

## Security

- All secrets loaded from `.env` (gitignored, owner-read only)
- Dashboard is password-protected; auth is session-based with brute-force lockout
- `run_command` tool has a hardened blocklist — kills, shutdown, sudo, reverse shells, env dumping, and encoded payloads are all refused
- Subprocess environment strips all secrets before execution
- `read_file` tool blocks access to credential files and SSH keys
- File access is restricted to the user's home directory
- `known_services.txt` is gitignored — never committed

Recommended deployment:
- Place behind Tailscale or another VPN; do not expose port 8080 to the internet
- UFW rule: allow port 8080 from your VPN subnet only
- fail2ban for defense-in-depth

---

## Data

- `data/` is gitignored — holds conversation history, agent output, calendar, and notes
- Agent output is written atomically (temp file + rename) to prevent blank files on kill
- Calendar events and notes persist across restarts

---

*Built for personal use on Raspberry Pi OS (64-bit). Use responsibly.*
