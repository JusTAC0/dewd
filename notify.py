"""
DEWD push alert helper via ntfy.sh.
Set NTFY_TOPIC in .env to enable. Leave blank to disable.
"""
import logging
import requests
from config import NTFY_URL, NTFY_TOPIC

log = logging.getLogger(__name__)


def send_alert(title: str, message: str, priority: str = "default") -> bool:
    if not NTFY_TOPIC:
        log.warning("[notify] NTFY_TOPIC is not set — skipping alert '%s'", title)
        return False
    try:
        resp = requests.post(
            f"{NTFY_URL}/{NTFY_TOPIC}",
            data=message,
            headers={
                "Title":        title,
                "Priority":     priority,
                "Tags":         "dewd",
                "Content-Type": "text/plain; charset=utf-8",
            },
            timeout=5,
        )
        resp.raise_for_status()
        log.info("[notify] sent '%s' → %s/%s", title, NTFY_URL, NTFY_TOPIC)
        return True
    except Exception as e:
        log.error("[notify] failed to send '%s': %s", title, e)
        return False
