"""
DEWD tool definitions and safe execution sandbox.

DEWD must never disable itself, kill its own process, shut down or
reboot the Raspberry Pi, or remove its own files.
"""
import json
import os
import re
import subprocess
import requests
from pathlib import Path

from config import DATA_DIR, BLUEPRINTS_DIR
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
    {
        "name": "list_blueprints",
        "description": "List all staged blueprints awaiting review and deployment.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "apply_blueprint",
        "description": (
            "Deploy a staged blueprint to live DEWD code after human approval. "
            "Runs safety scan, compile check, and cascade check before deploying. "
            "Restarts the DEWD service if successful."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "blueprint_id": {"type": "string", "description": "The blueprint ID to deploy (e.g. 'parallel-gather-optimization')."}
            },
            "required": ["blueprint_id"],
        },
    },
]


def execute_tool(name: str, inputs: dict) -> str:
    try:
        if name == "system_stats":       return _system_stats()
        elif name == "run_command":      return _run_command(inputs.get("command", ""))
        elif name == "get_weather":      return _get_weather(inputs.get("location", ""))
        elif name == "list_services":    return _list_services()
        elif name == "read_file":        return _read_file(inputs.get("path", ""))
        elif name == "list_blueprints":  return _list_blueprints()
        elif name == "apply_blueprint":  return _apply_blueprint(inputs.get("blueprint_id", ""))
        else:                            return f"Unknown tool: {name}"
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


def _list_blueprints() -> str:
    try:
        os.makedirs(BLUEPRINTS_DIR, exist_ok=True)
        files = [f for f in os.listdir(BLUEPRINTS_DIR) if f.endswith(".json")]
        if not files:
            return "No blueprints staged."
        lines = []
        for fn in sorted(files):
            try:
                with open(os.path.join(BLUEPRINTS_DIR, fn)) as f:
                    bp = json.load(f)
                status    = bp.get("status", "unknown")
                name      = bp.get("name", fn[:-5])
                bid       = bp.get("id", fn[:-5])
                score     = bp.get("score", "?")
                file_list = ", ".join(b["path"] for b in bp.get("files", []))
                lines.append(f"[{status.upper()}] {bid} — {name} (score:{score}) | files: {file_list}")
            except Exception:
                lines.append(fn)
        return "\n".join(lines)
    except Exception as e:
        return f"Could not list blueprints: {e}"


_DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
_VENV_PY       = os.path.join(_DASHBOARD_DIR, "venv", "bin", "python3")
_PYTHON_BIN    = _VENV_PY if os.path.exists(_VENV_PY) else "python3"

_BP_DANGEROUS = [
    (r"eval\s*\(",                          "eval()"),
    (r"exec\s*\(",                          "exec()"),
    (r"__import__\s*\(",                    "__import__()"),
    (r"os\.system\s*\(",                    "os.system()"),
    (r"subprocess\b[^\n]*shell\s*=\s*True", "subprocess shell=True"),
]


def _apply_blueprint(blueprint_id: str) -> str:
    if not blueprint_id.strip():
        return "ERROR: blueprint_id is required."

    bp_path = os.path.join(BLUEPRINTS_DIR, f"{blueprint_id}.json")
    if not os.path.exists(bp_path):
        return f"Blueprint '{blueprint_id}' not found. Use list_blueprints to see available ones."

    try:
        with open(bp_path) as f:
            bp = json.load(f)
    except Exception as e:
        return f"ERROR: Could not read blueprint: {e}"

    if bp.get("status") == "implemented":
        return f"Blueprint '{blueprint_id}' has already been implemented."
    if bp.get("status") == "failed":
        return f"Blueprint '{blueprint_id}' previously failed to apply. Check smith_log.md."

    files = bp.get("files", [])
    if not files:
        return "ERROR: Blueprint contains no files."

    # Safety scan all staged content
    for entry in files:
        content = entry.get("content", "")
        for pattern, label in _BP_DANGEROUS:
            if re.search(pattern, content, re.IGNORECASE):
                bp["status"] = "rejected"
                with open(bp_path, "w") as f:
                    json.dump(bp, f, indent=2)
                return f"REFUSED: Safety scan failed on {entry['path']} — {label} detected. Blueprint marked rejected."

    # Write files with .bak backup, track what we wrote for rollback
    written = []
    project_dir = _DASHBOARD_DIR
    try:
        for entry in files:
            rel     = entry["path"]
            content = entry["content"]
            live    = os.path.join(project_dir, rel)

            if not os.path.abspath(live).startswith(project_dir):
                return f"ERROR: Path traversal detected in blueprint file: {rel}"

            os.makedirs(os.path.dirname(live), exist_ok=True)
            if os.path.exists(live):
                with open(live) as f:
                    orig = f.read()
                with open(live + ".bak", "w") as f:
                    f.write(orig)

            with open(live, "w") as f:
                f.write(content)
            written.append(live)

            # Compile check
            result = subprocess.run(
                [_PYTHON_BIN, "-m", "py_compile", live],
                capture_output=True, text=True, timeout=15, cwd=project_dir,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Compile error in {rel}: {result.stderr.strip()}")

    except Exception as e:
        # Rollback all written files
        for live in written:
            bak = live + ".bak"
            if os.path.exists(bak):
                with open(bak) as f:
                    orig = f.read()
                with open(live, "w") as f:
                    f.write(orig)
        bp["status"] = "failed"
        with open(bp_path, "w") as f:
            json.dump(bp, f, indent=2)
        return f"FAILED: {e} — all files rolled back."

    # Mark implemented
    from datetime import datetime, timezone
    bp["status"]         = "implemented"
    bp["implemented_at"] = datetime.now(timezone.utc).isoformat()
    with open(bp_path, "w") as f:
        json.dump(bp, f, indent=2)

    file_list = ", ".join(entry["path"] for entry in files)

    # Restart service (notify before restart so the message gets through)
    from notify import send_alert
    send_alert(
        f"Blueprint Deployed — {bp.get('name', blueprint_id)}",
        f"Blueprint '{blueprint_id}' has been implemented successfully, Sir.\nFiles updated: {file_list}\nRestarting DEWD now.",
    )

    subprocess.Popen(
        ["sudo", "systemctl", "restart", "dewd"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    return (
        f"Blueprint '{blueprint_id}' deployed successfully.\n"
        f"Files updated: {file_list}\n"
        f"DEWD is restarting — you may need to refresh the dashboard."
    )


