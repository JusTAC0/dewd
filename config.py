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

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL      = "claude-haiku-4-5-20251001"  # swap back to claude-sonnet-4-6 if performance lacks
MAX_HISTORY_TURNS = 10                            # swap back to 25 if longer memory needed

GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS",      "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
GMAIL_MAX_MSGS     = 8

AGENTS_DIR = os.path.join(DATA_DIR, "agents")

NTFY_URL   = os.environ.get("NTFY_URL",   "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

WEATHER_LOCATION     = os.environ.get("WEATHER_LOCATION", "New York, NY")
OWNER_NAME           = os.environ.get("OWNER_NAME", "Sir")
SECRET_KEY           = os.environ.get("SECRET_KEY", "change-me-in-env")
DASHBOARD_PASSWORD   = os.environ.get("DASHBOARD_PASSWORD", "")

SYS_HISTORY_MAX = 48

# All schedules use Eastern local time (handles EST/EDT automatically)
# Sleep window is always 2am → start hour (no runs in that range)
TREND_SETTER_START_HOUR  = 6   # first run of day ET
TREND_SETTER_INTERVAL_HRS = 4

SYS_ANALYZER_START_HOUR  = 6
SYS_ANALYZER_INTERVAL_HRS = 4

DEV_SCOUT_START_HOUR     = 8
DEV_SCOUT_INTERVAL_HRS   = 4
