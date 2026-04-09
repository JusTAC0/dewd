"""
DEWD Configuration
Secrets are loaded from .env — never hardcode credentials here.
"""
import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

PROJECT_DIR     = os.path.dirname(os.path.abspath(__file__))
DATA_DIR        = os.path.join(PROJECT_DIR, "data")
LOG_FILE        = os.path.join(DATA_DIR, "conversation.json")
STATUS_FILE     = os.path.join(DATA_DIR, "status.json")
MAX_LOG_ENTRIES = 50

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL      = "claude-haiku-4-5-20251001"
MAX_HISTORY_TURNS = 10

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL   = "gpt-4o-mini"

GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS",      "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
GMAIL_MAX_MSGS     = 8

AGENTS_DIR    = os.path.join(DATA_DIR, "agents")
CALENDAR_FILE = os.path.join(DATA_DIR, "calendar.json")

NTFY_URL   = os.environ.get("NTFY_URL",   "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

WEATHER_LOCATION     = os.environ.get("WEATHER_LOCATION", "New York, NY")
OWNER_NAME           = os.environ.get("OWNER_NAME", "Sir")
SECRET_KEY           = os.environ.get("SECRET_KEY", "change-me-in-env")
DASHBOARD_PASSWORD   = os.environ.get("DASHBOARD_PASSWORD", "")

SYS_HISTORY_MAX = 48

SMITH_LOG_PATH = os.path.join(os.path.expanduser("~"), "Desktop", "smith_log.md")

# Daymark — world awareness, 3x daily
DAYMARK_HOURS = (7, 13, 19)   # 7am, 1pm, 7pm ET

# Morning chain hour — Daymark run that kicks off Frontier → Smith → brief
MORNING_CHAIN_HOUR = 7

# Frontier — tech scouting, 2x daily
FRONTIER_HOURS = (9, 21)       # 9am, 9pm ET

# Smith — triggered by Frontier, no fixed schedule
# Morning brief window: if Smith runs between these hours ET it fires the brief
SMITH_BRIEF_WINDOW = (8, 12)   # 8am–12pm ET
