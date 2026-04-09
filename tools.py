"""
DEWD tool definitions and safe execution sandbox.

DEWD must never disable itself, kill its own process, shut down or
reboot the Raspberry Pi, or remove its own files.
"""
import os
import re
import subprocess
import requests
from pathlib import Path

from config import DATA_DIR
from logger import get_logger

log = get_logger(__name__)

_HOME = str(Path.home())

_FORBIDDEN = [
    r"pkill", r"killall", r"kill\s+-", r"kill\s+\d",
    r"pgrep.*dewd", r"pgrep.*python",
    r"\bshutdown\b", r"\bpoweroff\b", r"\bhalt\b", r"\breboot\b", r"init\s+[06]",
    r"systemctl\s+(stop|disable|kill|mask|reset-failed)",
    r"rm\s+.*-[rf]", r"rm\s+-[rf]", r"mkfs", r"dd\s+if=", r">\s*/dev/sd",
    r"shred\b", r"wipefs\b",
    r"\bprintenv\b", r"\benv\b", r"\bexport\b", r"\bset\b\s*$",
    r"python[23]?\s+-c\b", r"\bperl\s+-e\b", r"\bruby\s+-e\b", r"\bnode\s+-e\b",
    r"base64\s+.*\|\s*(bash|sh|python)", r"\|\s*(bash|sh)\s*$",
    r"cat\s+.*\.env", r"cat\s+.*config\.py", r"cat\s+.*\.ssh",
    r"cat\s+.*id_rsa", r"cat\s+.*id_ed25519",
    r"curl\s+.*\|\s*(bash|sh|python)", r"wget\s+.*\|\s*(bash|sh|python)",
    r"\biptables\b", r"\bnftables\b", r"\bufw\b",
    r"\bsudo\b", r"\bsu\s", r"\bchmod\s+[0-7]*7", r"\bchown\b",
    r"\bpasswd\b", r"\bchpasswd\b",
    r"crontab\s+-[re]", r"/etc/cron", r"\.bashrc|\.bash_profile|\.profile",
    r"/dev/tcp", r"/dev/udp", r"\bnc\s.*-[el]", r"bash\s+-i",
    r"/proc/self/environ", r"/proc/[^/]+/environ",
]

_BLOCKED_PATHS = [
    ".env", "config.py", ".ssh", ".gnupg", ".netrc", ".git/config",
    "id_rsa", "id_ed25519", "id_ecdsa", "authorized_keys",
]

_SECRET_ENV_KEYS = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GMAIL_APP_PASSWORD", "GMAIL_ADDRESS"}


def _is_safe(command: str) -> tuple[bool, str]:
    low = command.lower()
    for pattern in _FORBIDDEN:
        if re.search(pattern, low):
            return False, f"Blocked by safety policy: matches '{pattern}'"
    return True, ""


def _clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in _SECRET_ENV_KEYS}


TOOL_DEFINITIONS = [
    {
        "name": "system_stats",
        "description": "Get current Raspberry Pi system status: CPU usage, RAM, disk space, and CPU temperature.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "run_command",
        "description": (
            "Run a shell command on the Pi and return its output. "
            "You may NOT use this to disable DEWD, shut down the Pi, or "
            "delete system files — such commands will be refused."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "The shell command to run."}},
            "required": ["command"],
        },
    },
    {
        "name": "get_weather",
        "description": "Get the current weather conditions for a location.",
        "input_schema": {
            "type": "object",
            "properties": {"location": {"type": "string", "description": "City name or location (e.g. 'New York, NY')."}},
            "required": ["location"],
        },
    },
    {
        "name": "list_services",
        "description": "List running systemd user services and their status.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "read_file",
        "description": "Read a text file from the filesystem. Only files under the home directory are accessible.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute file path."}},
            "required": ["path"],
        },
    },
]


def execute_tool(name: str, inputs: dict) -> str:
    try:
        if name == "system_stats":    return _system_stats()
        elif name == "run_command":   return _run_command(inputs.get("command", ""))
        elif name == "get_weather":   return _get_weather(inputs.get("location", ""))
        elif name == "list_services": return _list_services()
        elif name == "read_file":     return _read_file(inputs.get("path", ""))
        else:                         return f"Unknown tool: {name}"
    except Exception as e:
        return f"Tool error ({name}): {e}"


def _system_stats() -> str:
    from services.stats import get_stats, format_for_tool
    return format_for_tool(get_stats())


def _run_command(command: str) -> str:
    if not command.strip():
        return "No command provided."
    safe, reason = _is_safe(command)
    if not safe:
        return f"Refused. {reason}"
    proc = subprocess.Popen(
        command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=_clean_env(),
    )
    try:
        stdout, stderr = proc.communicate(timeout=15)
        output = (stdout + stderr).strip()
        return output if output else "(no output)"
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return "Command timed out after 15 seconds."


def _get_weather(location: str) -> str:
    if not location:
        return "No location specified."
    try:
        resp = requests.get(
            f"https://wttr.in/{requests.utils.quote(location)}",
            params={"format": "3"}, timeout=8,
        )
        resp.raise_for_status()
        return resp.text.strip()
    except Exception as e:
        return f"Could not fetch weather: {e}"


def _list_services() -> str:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "list-units", "--type=service",
             "--state=active", "--no-pager", "--no-legend"],
            capture_output=True, text=True, timeout=10,
        )
        lines = [l.split()[0] for l in result.stdout.strip().splitlines() if l.strip()]
        return "Active services: " + ", ".join(lines) if lines else "No active user services."
    except Exception as e:
        return f"Could not list services: {e}"


def _read_file(path: str) -> str:
    real = os.path.realpath(path)
    if not real.startswith(_HOME):
        return f"Access denied: only files under {_HOME} are readable."
    basename = os.path.basename(real).lower()
    for blocked in _BLOCKED_PATHS:
        if blocked.lower() == basename or real.lower().endswith(os.sep + blocked.lower()):
            return f"Access denied: {os.path.basename(real)} is a protected file."
    try:
        with open(real, "r", errors="replace") as f:
            content = f.read(4000)
        return content if content else "(file is empty)"
    except FileNotFoundError:
        return f"File not found: {path}"
    except Exception as e:
        return f"Could not read file: {e}"


