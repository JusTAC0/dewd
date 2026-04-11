"""
DEWD Gmail service — IMAP fetch, body retrieval, and delete.
"""
import email
import email.utils
import imaplib
import re
from email.header import decode_header as _decode_header
from html.parser import HTMLParser as _HTMLParser
from datetime import timezone

from config import GMAIL_ADDRESS, GMAIL_APP_PASSWORD, GMAIL_MAX_MSGS
from logger import get_logger

log = get_logger(__name__)


def _decode_str(raw: str) -> str:
    parts = _decode_header(raw or "")
    out = []
    for b, enc in parts:
        if isinstance(b, bytes):
            out.append(b.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(b)
    return "".join(out)


def _strip_html(html_str: str) -> str:
    class _S(_HTMLParser):
        _SKIP_TAGS  = {"style", "script", "head"}
        _BLOCK_OPEN = {"p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "br", "hr", "blockquote", "pre"}
        _BLOCK_CLOSE = {"p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"}

        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []
            self._depth = 0

        def handle_data(self, d):
            if self._depth == 0:
                self.parts.append(d)

        def handle_starttag(self, tag, attrs):
            if tag in self._SKIP_TAGS:
                self._depth += 1
                return
            if self._depth == 0 and tag in self._BLOCK_OPEN:
                self.parts.append("\n")

        def handle_endtag(self, tag):
            if tag in self._SKIP_TAGS:
                self._depth = max(0, self._depth - 1)
                return
            if self._depth == 0 and tag in self._BLOCK_CLOSE:
                self.parts.append("\n")

    s = _S()
    s.feed(html_str)
    text = "".join(s.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _connect() -> imaplib.IMAP4_SSL:
    mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=10)
    mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
    return mail


def fetch_inbox() -> dict:
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return {"configured": False, "unread": 0, "emails": []}
    mail = None
    try:
        mail = _connect()
        mail.select("INBOX", readonly=True)
        _, udata = mail.search(None, "UNSEEN")
        unread_ids = udata[0].split() if udata[0] else []
        _, adata = mail.search(None, "ALL")
        all_ids = adata[0].split() if adata[0] else []
        fetch_ids = all_ids[-GMAIL_MAX_MSGS:] if len(all_ids) >= GMAIL_MAX_MSGS else all_ids
        fetch_ids = fetch_ids[::-1]
        unread_set = set(unread_ids)
        msgs = []
        for uid in fetch_ids:
            _, data = mail.fetch(uid, "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if not data or not data[0]:
                continue
            raw_header = data[0][1] if isinstance(data[0], tuple) else b""
            msg = email.message_from_bytes(raw_header)
            subject  = _decode_str(msg.get("Subject", "(no subject)"))
            sender   = _decode_str(msg.get("From", ""))
            date_str = msg.get("Date", "")
            try:
                parsed = email.utils.parsedate_to_datetime(date_str)
                ts_iso = parsed.astimezone(timezone.utc).isoformat()
            except Exception:
                ts_iso = ""
            msgs.append({
                "uid":     uid.decode(),
                "subject": subject[:80],
                "from":    sender[:60],
                "ts":      ts_iso,
                "unread":  uid in unread_set,
            })
        return {"configured": True, "unread": len(unread_ids), "emails": msgs}
    except Exception as e:
        log.warning("fetch_inbox error: %s", e)
        return {"configured": True, "error": str(e), "unread": 0, "emails": []}
    finally:
        if mail:
            try:
                mail.logout()
            except Exception:
                pass


def fetch_body(uid: str) -> dict:
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return {"error": "not configured"}
    if not (uid.isascii() and uid.isdigit()):
        return {"error": "invalid uid"}
    mail = None
    try:
        mail = _connect()
        mail.select("INBOX", readonly=True)
        _, data = mail.fetch(uid.encode(), "(RFC822)")
        if not data or not data[0]:
            return {"error": "message not found"}
        raw = data[0][1]
        msg = email.message_from_bytes(raw)
        subject  = _decode_str(msg.get("Subject", ""))
        sender   = _decode_str(msg.get("From", ""))
        date_str = msg.get("Date", "")
        try:
            parsed = email.utils.parsedate_to_datetime(date_str)
            ts_iso = parsed.astimezone(timezone.utc).isoformat()
        except Exception:
            ts_iso = ""
        body = ""
        html_fallback = ""
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                cd = str(part.get("Content-Disposition", ""))
                if "attachment" in cd:
                    continue
                if ct == "text/plain" and not body:
                    raw_b = part.get_payload(decode=True) or b""
                    body = raw_b.decode(part.get_content_charset() or "utf-8", errors="replace")
                elif ct == "text/html" and not html_fallback:
                    raw_b = part.get_payload(decode=True) or b""
                    html_fallback = raw_b.decode(part.get_content_charset() or "utf-8", errors="replace")
        else:
            raw_b = msg.get_payload(decode=True) or b""
            text  = raw_b.decode(msg.get_content_charset() or "utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                html_fallback = text
            else:
                body = text
        if not body and html_fallback:
            body = _strip_html(html_fallback)
        return {"subject": subject, "from": sender, "ts": ts_iso, "body": body[:6000]}
    except Exception as e:
        log.warning("fetch_body(%s) error: %s", uid, e)
        return {"error": str(e)}
    finally:
        if mail:
            try:
                mail.logout()
            except Exception:
                pass


def delete_message(uid: str) -> dict:
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return {"error": "not configured"}
    if not (uid.isascii() and uid.isdigit()):
        return {"error": "invalid uid"}
    mail = None
    try:
        mail = _connect()
        mail.select("INBOX")
        mail.copy(uid.encode(), "[Gmail]/Trash")
        mail.store(uid.encode(), "+FLAGS", "\\Deleted")
        mail.expunge()
        return {"ok": True}
    except Exception as e:
        log.warning("delete_message(%s) error: %s", uid, e)
        return {"error": str(e)}
    finally:
        if mail:
            try:
                mail.logout()
            except Exception:
                pass
