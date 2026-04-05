"""
DEWD push alert helper via ntfy.sh.
Set NTFY_TOPIC in .env to enable. Leave blank to disable.
"""
import requests
from config import NTFY_URL, NTFY_TOPIC


def send_alert(title: str, message: str, priority: str = "default") -> bool:
    if not NTFY_TOPIC:
        return False
    try:
        resp = requests.post(
            f"{NTFY_URL}/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": "dewd"},
            timeout=5,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[notify] {e}")
        return False
