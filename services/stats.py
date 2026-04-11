"""
DEWD system stats service — single authoritative source for Pi hardware metrics.
Used by both the dashboard API and the chat tool.
"""
import os
import subprocess
import shutil
import time

from logger import get_logger

log = get_logger(__name__)


def get_stats() -> dict:
    """Return a dict of current Pi hardware metrics."""
    stats: dict = {}

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
            ifaces.append({
                "name":  name,
                "rx_mb": round(int(parts[1]) / 1e6, 1),
                "tx_mb": round(int(parts[9]) / 1e6, 1),
            })
        stats["interfaces"] = ifaces
    except Exception:
        stats["interfaces"] = []

    return stats


def format_for_tool(stats: dict) -> str:
    """Format stats dict as a human-readable string for the chat tool."""
    lines = [
        f"CPU: {stats.get('cpu_pct', '—')}%",
        f"CPU temp: {stats.get('temp', '—')}",
        f"RAM: {stats.get('ram_used_mb', '—')} MB used / {stats.get('ram_total_mb', '—')} MB total ({stats.get('ram_pct', '—')}%)",
        f"Disk: {stats.get('disk_used_gb', '—')} GB used / {stats.get('disk_total_gb', '—')} GB total ({stats.get('disk_pct', '—')}%)",
        f"Uptime: {stats.get('uptime', '—')}",
    ]
    for iface in stats.get("interfaces", []):
        lines.append(f"Network {iface['name']}: RX {iface['rx_mb']} MB / TX {iface['tx_mb']} MB")
    return "\n".join(lines)
