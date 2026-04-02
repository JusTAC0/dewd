"""
DEWD System Analyzer Agent

Collects hardware and network telemetry from the Pi, then uses
Claude Haiku for intelligent analysis and anomaly detection.
Writes results to data/agents/system_analyzer.json
"""
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone

import anthropic

from config import ANTHROPIC_API_KEY, DATA_DIR, SYS_HISTORY_MAX

try:
    from notify import send_alert as _ntfy
except Exception:
    def _ntfy(*a, **kw): return False

HAIKU_MODEL = "claude-haiku-4-5-20251001"
AGENTS_DIR  = os.path.join(DATA_DIR, "agents")
OUTPUT_FILE = os.path.join(AGENTS_DIR, "system_analyzer.json")


def _collect_hardware() -> dict:
    hw = {}
    try:
        raw = subprocess.check_output(["vcgencmd", "measure_temp"], text=True).strip()
        hw["cpu_temp"] = raw.replace("temp=", "")
    except Exception:
        hw["cpu_temp"] = "unavailable"
    try:
        with open("/proc/stat") as f: c0 = f.readline().split()
        time.sleep(1)
        with open("/proc/stat") as f: c1 = f.readline().split()
        idle0, total0 = int(c0[4]), sum(int(x) for x in c0[1:])
        idle1, total1 = int(c1[4]), sum(int(x) for x in c1[1:])
        hw["cpu_pct"] = round(100.0 * (1 - (idle1-idle0)/(total1-total0)), 1)
    except Exception:
        hw["cpu_pct"] = None
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        hw["load_1m"], hw["load_5m"], hw["load_15m"] = parts[0], parts[1], parts[2]
    except Exception:
        pass
    try:
        with open("/proc/meminfo") as f:
            mem = {l.split(":")[0]: int(l.split()[1]) for l in f}
        hw["ram_total_mb"] = mem["MemTotal"] // 1024
        hw["ram_avail_mb"] = mem["MemAvailable"] // 1024
        hw["ram_used_mb"]  = hw["ram_total_mb"] - hw["ram_avail_mb"]
        hw["ram_pct"]      = round(hw["ram_used_mb"] / hw["ram_total_mb"] * 100, 1)
        if "SwapTotal" in mem and mem["SwapTotal"] > 0:
            hw["swap_total_mb"] = mem["SwapTotal"] // 1024
            hw["swap_used_mb"]  = (mem["SwapTotal"] - mem.get("SwapFree", mem["SwapTotal"])) // 1024
    except Exception:
        pass
    try:
        du = shutil.disk_usage("/")
        hw["disk_total_gb"] = round(du.total / 1e9, 1)
        hw["disk_used_gb"]  = round(du.used  / 1e9, 1)
        hw["disk_pct"]      = round(du.used / du.total * 100, 1)
    except Exception:
        pass
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
        h, m = divmod(int(secs) // 60, 60)
        hw["uptime"] = f"{h}h {m}m"
    except Exception:
        pass
    return hw


def _collect_network_interfaces() -> list[dict]:
    ifaces = []
    try:
        with open("/proc/net/dev") as f:
            lines = f.readlines()[2:]
        for line in lines:
            parts = line.split()
            if len(parts) < 10:
                continue
            name = parts[0].rstrip(":")
            if name == "lo":
                continue
            ifaces.append({
                "name":       name,
                "rx_bytes":   int(parts[1]),
                "rx_packets": int(parts[2]),
                "rx_errors":  int(parts[3]),
                "rx_dropped": int(parts[4]),
                "tx_bytes":   int(parts[9]),
                "tx_packets": int(parts[10]),
                "tx_errors":  int(parts[11]),
            })
    except Exception:
        pass
    return ifaces


def _collect_active_connections() -> list[dict]:
    conns = []
    try:
        result = subprocess.run(
            ["sudo", "ss", "-tunap", "--no-header"],
            capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) < 6:
                continue
            proto  = parts[0]
            state  = parts[1] if proto == "tcp" else "—"
            local  = parts[4]
            remote = parts[5]
            process = ""
            for p in parts[6:]:
                m = re.search(r'"([^"]+)"', p)
                if m:
                    process = m.group(1)
                    break
            if remote.startswith("127.") or remote in ("*:*", "0.0.0.0:*") or remote.startswith("[::"):
                continue
            conns.append({"proto": proto, "state": state, "local": local, "remote": remote, "process": process})
    except Exception:
        pass
    return conns


def _collect_listening_ports() -> list[dict]:
    ports = []
    try:
        result = subprocess.run(
            ["sudo", "ss", "-tlnp", "--no-header"],
            capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            local = parts[3]
            process = ""
            for p in parts[4:]:
                m = re.search(r'"([^"]+)"', p)
                if m:
                    process = m.group(1)
                    break
            ports.append({"local": local, "process": process})
    except Exception:
        pass
    return ports


def _collect_wifi() -> dict:
    wifi = {}
    try:
        result = subprocess.run(["iwconfig"], capture_output=True, text=True, timeout=5)
        text = result.stdout + result.stderr
        for line in text.splitlines():
            if "ESSID" in line:
                m = re.search(r'ESSID:"([^"]*)"', line)
                if m: wifi["ssid"] = m.group(1)
            if "Signal level" in line:
                m = re.search(r"Signal level=(-?\d+)", line)
                if m: wifi["signal_dbm"] = int(m.group(1))
            if "Bit Rate" in line:
                m = re.search(r"Bit Rate=(\S+ [MG]b/s)", line)
                if m: wifi["bit_rate"] = m.group(1)
    except Exception:
        pass
    try:
        r = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True, timeout=5)
        m = re.search(r"default via (\S+)", r.stdout)
        if m: wifi["gateway"] = m.group(1)
    except Exception:
        pass
    return wifi


def _collect_top_processes(n: int = 12) -> list[dict]:
    procs = []
    try:
        result = subprocess.run(
            ["ps", "aux", "--sort=-%cpu"],
            capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.strip().splitlines()[1:n+1]:
            parts = line.split(None, 10)
            if len(parts) < 11:
                continue
            procs.append({"user": parts[0], "pid": parts[1], "cpu_pct": parts[2], "mem_pct": parts[3], "cmd": parts[10][:80]})
    except Exception:
        pass
    return procs


def _collect_auth_log(lines: int = 30) -> list[str]:
    for path in ["/var/log/auth.log", "/var/log/secure"]:
        try:
            result = subprocess.run(["tail", f"-{lines}", path], capture_output=True, text=True, timeout=5)
            if result.stdout:
                return result.stdout.strip().splitlines()
        except Exception:
            continue
    try:
        result = subprocess.run(
            ["journalctl", "-u", "sshd", "-n", str(lines), "--no-pager", "--output=short"],
            capture_output=True, text=True, timeout=5
        )
        if result.stdout:
            return result.stdout.strip().splitlines()
    except Exception:
        pass
    return []


def _collect_syslog(lines: int = 30) -> list[str]:
    for path in ["/var/log/syslog", "/var/log/messages"]:
        try:
            result = subprocess.run(["tail", f"-{lines}", path], capture_output=True, text=True, timeout=5)
            if result.stdout:
                return result.stdout.strip().splitlines()
        except Exception:
            continue
    try:
        result = subprocess.run(
            ["journalctl", "-n", str(lines), "--no-pager", "--output=short", "-p", "warning"],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip().splitlines() if result.stdout else []
    except Exception:
        return []


def _collect_services() -> list[dict]:
    services = []
    try:
        result = subprocess.run(
            ["systemctl", "--user", "list-units", "--type=service", "--no-pager", "--no-legend", "--all"],
            capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.strip().splitlines():
            parts = line.split(None, 4)
            if len(parts) >= 4:
                services.append({
                    "name": parts[0], "load": parts[1], "active": parts[2],
                    "sub": parts[3], "desc": parts[4] if len(parts) > 4 else "",
                })
    except Exception:
        pass
    return services


def _collect_security_posture() -> dict:
    posture = {}
    # UFW status
    try:
        r = subprocess.run(["sudo", "ufw", "status", "verbose"],
                           capture_output=True, text=True, timeout=5)
        posture["ufw"] = r.stdout.strip()[:800] if r.returncode == 0 else "unavailable"
    except Exception:
        posture["ufw"] = "unavailable"
    # SSH service state
    try:
        r = subprocess.run(["systemctl", "is-active", "ssh"],
                           capture_output=True, text=True, timeout=5)
        posture["ssh_service"] = r.stdout.strip()  # "inactive" = disabled intentionally
    except Exception:
        posture["ssh_service"] = "unknown"
    # fail2ban
    try:
        r = subprocess.run(["sudo", "fail2ban-client", "status"],
                           capture_output=True, text=True, timeout=5)
        posture["fail2ban"] = r.stdout.strip()[:400] if r.returncode == 0 else "unavailable"
    except Exception:
        posture["fail2ban"] = "unavailable"
    # unattended-upgrades
    try:
        r = subprocess.run(["systemctl", "is-active", "unattended-upgrades"],
                           capture_output=True, text=True, timeout=5)
        posture["unattended_upgrades"] = r.stdout.strip()
    except Exception:
        posture["unattended_upgrades"] = "unknown"
    return posture


def _collect_failed_logins() -> dict:
    counts = {"ssh_failures": 0, "sudo_failures": 0, "unique_ips": []}
    ips = set()
    for line in _collect_auth_log(100):
        if "Failed password" in line or "Invalid user" in line:
            counts["ssh_failures"] += 1
            m = re.search(r"from (\d+\.\d+\.\d+\.\d+)", line)
            if m: ips.add(m.group(1))
        if "sudo" in line and "incorrect password" in line.lower():
            counts["sudo_failures"] += 1
    counts["unique_ips"] = list(ips)[:20]
    return counts


def collect_all() -> dict:
    return {
        "collected_at":       datetime.now(timezone.utc).isoformat(),
        "hardware":           _collect_hardware(),
        "network_interfaces": _collect_network_interfaces(),
        "active_connections": _collect_active_connections(),
        "listening_ports":    _collect_listening_ports(),
        "wifi":               _collect_wifi(),
        "top_processes":      _collect_top_processes(),
        "services":           _collect_services(),
        "failed_logins":      _collect_failed_logins(),
        "recent_syslog":      _collect_syslog(30),
        "security_posture":   _collect_security_posture(),
    }


_KNOWN_SERVICES = """
KNOWN LEGITIMATE SERVICES ON THIS SYSTEM (do not flag as anomalies):
- pironman5 / pironman5-service (pid varies): Pi case fan/RGB controller — listens on 0.0.0.0:34001, run by root. EXPECTED.
- openclaw-gateway / openclaw-gatewa: AI coding CLI tool installed by user at ~/.openclaw/ — makes HTTPS connections, uses mDNS (port 5353 UDP). EXPECTED.
- rpi-connectd: Raspberry Pi Connect remote access daemon — connects to 185.101.x.x and 176.126.x.x on port 443/3478. EXPECTED.
- tailscaled: Tailscale VPN — listens on port 41641 UDP, 100.x.x.x addresses, and a high TCP port. EXPECTED.
- avahi-daemon: mDNS/zeroconf — port 5353 UDP, 0.0.0.0. EXPECTED.
- DEWD itself (python3 dewd_web.py): Flask dashboard on 0.0.0.0:8080. EXPECTED.
- sshd: SSH on 0.0.0.0:22 — protected by UFW allowlist and fail2ban. EXPECTED.
- VS Code / code: IDE remote server — connects to Microsoft IPs (13.107.x.x) on 443. EXPECTED.

SECURITY CONTROLS IN PLACE:
- SSH (sshd) is INTENTIONALLY DISABLED — not in use, not needed. Port 22 will not appear. fail2ban sshd jail may still appear in status output but SSH is not running. Do NOT penalize for this; it is a deliberate hardening step that improves security.
- UFW firewall: deny all incoming by default. Only port 8080 allowed, restricted to Tailscale subnet 100.64.0.0/10 only (not open to general LAN or internet).
- Access is via Tailscale VPN or Raspberry Pi Connect screen share — no SSH needed.
- fail2ban installed for defense-in-depth even though SSH is off.
- unattended-upgrades for automatic security patches.
- SSH X11Forwarding disabled.
- InfluxDB bound to localhost only.
- rpcbind disabled (port 111 removed).
- eth0 RX dropped packets in the 90k-100k range are a KNOWN RASPBERRY PI 5 DRIVER CHARACTERISTIC, not a security issue or hardware fault. Do not flag as anomaly.
Account for all these controls when assigning the HEALTH SCORE. A system with SSH disabled, VPN-only access, and UFW restricted to one port/subnet is well-hardened and should score accordingly.
"""

_SYSTEM_CACHED = [{
    "type": "text",
    "text": "You are DEWD's System Analyzer — a concise, technical security and health agent. "
            "You receive raw system telemetry from a Raspberry Pi 5 and produce a structured analysis report. "
            "Be precise. Flag real anomalies. Do not pad with generic advice. Use plain text, no markdown headers.\n"
            + _KNOWN_SERVICES,
    "cache_control": {"type": "ephemeral"},
}]


def _build_prompt(raw: dict) -> str:
    payload = json.dumps(raw, indent=2)[:12000]
    return f"""Analyze this Raspberry Pi 5 system telemetry and produce a structured report.

TELEMETRY:
{payload}

---

Produce exactly these sections:

HEALTH SCORE: [0-100] — overall system health

HARDWARE STATUS:
- CPU, RAM, disk, temperature — state each metric, flag anything abnormal

NETWORK TRAFFIC SUMMARY:
- Bytes in/out per interface, active external connections, which processes are reaching out
- List any unexpected or suspicious remote IPs/ports

SECURITY CHECK:
- Failed login attempts, unusual ports open, suspicious processes
- Note any connections to non-standard external IPs
- Note active security controls (UFW rules, fail2ban status, auto-updates)

SERVICE STATUS:
- Any failed or degraded services

ANOMALIES: (list or "None detected")

SUMMARY: 2-3 sentence plain-English overview for the system owner."""


def analyze(raw: dict) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=HAIKU_MODEL, max_tokens=1800,
        system=_SYSTEM_CACHED,
        messages=[{"role": "user", "content": _build_prompt(raw)}],
    )
    return msg.content[0].text


def analyze_stream(raw: dict):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    with client.messages.stream(
        model=HAIKU_MODEL, max_tokens=1800,
        system=_SYSTEM_CACHED,
        messages=[{"role": "user", "content": _build_prompt(raw)}],
    ) as stream:
        for text in stream.text_stream:
            yield text


def _check_alerts(hw: dict, report: str):
    alerts = []
    temp_str = hw.get("cpu_temp", "0").replace("'C", "").replace("°C", "").strip()
    try:
        temp_val = float(temp_str)
        if temp_val >= 80:   alerts.append(f"CPU temp critical: {temp_val}°C")
        elif temp_val >= 75: alerts.append(f"CPU temp high: {temp_val}°C")
    except Exception:
        pass
    if hw.get("disk_pct", 0) >= 85: alerts.append(f"Disk usage high: {hw['disk_pct']}%")
    if hw.get("ram_pct", 0) >= 90:  alerts.append(f"RAM usage critical: {hw['ram_pct']}%")
    if report and "ANOMALIES:" in report:
        section = report.split("ANOMALIES:")[1].split("\n\n")[0].strip()
        if section and "None detected" not in section:
            alerts.append("Anomalies detected — check Intel report")
    if alerts:
        critical = any(kw in a.lower() for a in alerts for kw in ("critical", "anomalies"))
        _ntfy("SysAna Alert — DEWD", "\n".join(alerts), priority="high" if critical else "default")


def _append_history(hw: dict):
    hist_file = os.path.join(AGENTS_DIR, "system_analyzer_history.json")
    try:
        try:
            with open(hist_file) as f: history = json.load(f)
        except Exception:
            history = []
        temp_str = hw.get("cpu_temp", "0").replace("'C", "").replace("°C", "").strip()
        try:    temp_val = float(temp_str)
        except Exception: temp_val = 0.0
        history.append({
            "ts":       datetime.now(timezone.utc).isoformat(),
            "cpu_pct":  hw.get("cpu_pct") or 0,
            "ram_pct":  hw.get("ram_pct") or 0,
            "disk_pct": hw.get("disk_pct") or 0,
            "temp_c":   temp_val,
        })
        with open(hist_file, "w") as f:
            json.dump(history[-SYS_HISTORY_MAX:], f)
    except Exception as e:
        print(f"[system_analyzer] history append failed: {e}")


def _atomic_write(path: str, data: dict):
    """Write JSON atomically — temp file + rename so a killed process never leaves a blank file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _write_status(state: str):
    os.makedirs(AGENTS_DIR, exist_ok=True)
    try:
        existing = {}
        if os.path.exists(OUTPUT_FILE):
            with open(OUTPUT_FILE) as f: existing = json.load(f)
        existing["status"] = state
        _atomic_write(OUTPUT_FILE, existing)
    except Exception:
        pass


def run() -> dict:
    os.makedirs(AGENTS_DIR, exist_ok=True)
    _write_status("running")
    try:
        raw    = collect_all()
        report = analyze(raw)
        _append_history(raw["hardware"])
        _check_alerts(raw["hardware"], report)
        result = {
            "status":       "ok",
            "ran_at":       raw["collected_at"],
            "report":       report,
            "raw_snapshot": {
                "hardware":           raw["hardware"],
                "active_connections": raw["active_connections"][:20],
                "failed_logins":      raw["failed_logins"],
                "wifi":               raw["wifi"],
            },
        }
    except Exception as e:
        result = {
            "status": "error",
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "error":  str(e),
            "report": f"Analysis failed: {e}",
        }
    _atomic_write(OUTPUT_FILE, result)
    return result


if __name__ == "__main__":
    print(run()["report"])
