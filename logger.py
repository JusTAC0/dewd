"""
DEWD centralised logging configuration.
All modules import `get_logger(__name__)` instead of using print().
Logs to stderr (captured by systemd) and to data/dewd.log with rotation.
"""
import logging
import logging.handlers
import os

from config import DATA_DIR

_LOG_FILE    = os.path.join(DATA_DIR, "dewd.log")
_MAX_BYTES   = 2 * 1024 * 1024
_BACKUP_COUNT = 3

_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

def _configure():
    os.makedirs(DATA_DIR, exist_ok=True)

    root = logging.getLogger()
    if root.handlers:
        return

    root.setLevel(logging.INFO)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter(_FMT, _DATE_FMT))
    root.addHandler(stream_handler)

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            _LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(_FMT, _DATE_FMT))
        root.addHandler(file_handler)
    except Exception as e:
        root.warning("Could not open log file %s: %s", _LOG_FILE, e)

    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)


_configure()


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
