"""Live SMS only.

Matrix SMS Report (/api/sms) ko poll karta hai aur sirf NAYI entries
Telegram group me bhejta hai. Kuch aur nahi:
  - payout nahi
  - DM nahi (koi DM reply/subscription/warning nahi, getUpdates bhi nahi)
  - stats / /update nahi
  - CLI hunt nahi
  - announcements nahi
  - premium emoji har jagah (reject hone par plain fallback)
  - disk write nahi; sab state memory me, caps ke saath
"""

from __future__ import annotations

import zlib
import html
import logging
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests


def _load_env(path: Path) -> None:
    """Minimal .env reader — extra dependency ke bina."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(
            key.strip(), value.strip().strip('"').strip("'")
        )


_load_env(Path(__file__).with_name(".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("live-sms")


class _NoRetry(RuntimeError):
    """Permanent client error — retry se faayda nahi, turant fallback karo."""


def _backoff(attempt: int) -> None:
    time.sleep(RETRY_BASE * (2 ** attempt) + random.uniform(0, 0.15))


def _retry(fn, attempts: int | None = None):
    """Transient errors par exponential backoff; last error phir raise."""
    if attempts is None:
        attempts = REQ_RETRIES
    last = None
    for i in range(attempts):
        try:
            return fn()
        except _NoRetry:
            raise
        except Exception as exc:
            last = exc
            if i + 1 < attempts:
                _backoff(i)
    raise last


BASE_URL = os.getenv("PANEL_URL", "https://matrixpanel.io").rstrip("/").replace(
    "://www.matrixpanel.io", "://matrixpanel.io"
)
LOGIN_PATH = "/auth/login"
SMS_API_PATH = "/api/sms"
USERNAME = os.getenv("PANEL_USERNAME", "").strip()
PASSWORD = os.getenv("PANEL_PASSWORD", "").strip()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = (
    os.getenv("SMS_LIVE_CHAT", "").strip()
    or os.getenv("UPDATE_CHAT_IDS", "").split(",")[0].strip()
    or os.getenv("DEFAULT_CHAT_ID", "").strip()
).strip()
POLL_SEC = max(1, int(os.getenv("POLL_SEC", "2")))
SEND_DELAY_SEC = max(0.05, float(os.getenv("SEND_DELAY_SEC", "0.2")))
FETCH_LIMIT = max(25, min(1000, int(os.getenv("FETCH_LIMIT", "100"))))
PREMIUM_EMOJI = os.getenv("PREMIUM_EMOJI", "1").strip().lower() not in (
    "0", "off", "false", "no"
)
# Restart ke baad boot se thoda pehle tak ki SMS bhi pakdo (downtime gap cover).
RESTART_CATCHUP_SEC = max(0, int(os.getenv("RESTART_CATCHUP_SEC", "30")))

# CLI search/sharding: Live Feed (/api/live_feed) se CLI names nikaal kar
# 3 services me split karna hai. Har service apne hisse ko CLI_CHECK_SEC
# antar par check karti hai (~20 CLI/min). Sab same group me bhejti hain.
CLI_SEARCH = os.getenv("CLI_SEARCH", "1").strip().lower() not in (
    "0", "off", "false", "no"
)
LIVE_FEED_PATH = "/api/live_feed"
CLI_LIST_REFRESH_SEC = max(60, int(os.getenv("CLI_LIST_REFRESH_SEC", "300")))
CLI_SHARD_INDEX = max(0, int(os.getenv("CLI_SHARD_INDEX", "0")))
CLI_SHARD_COUNT = max(1, int(os.getenv("CLI_SHARD_COUNT", "1")))
CLI_CHECK_SEC = max(0.2, float(os.getenv("CLI_CHECK_SEC", "3")))

BOOT_UTC = datetime.now(timezone.utc)
PORT = int(os.getenv("PORT", "10000"))
# Bot khud har 1-2 minute me apne /health ko ping karta hai (random interval).
# Render har web service ko RENDER_EXTERNAL_URL deta hai — kisi bhi account/URL par
# seedha apna /health pakad leta hai, hard-coded domain nahi.
_render_url = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
SELF_PING_URL = os.getenv("SELF_PING_URL", "").strip() or (
    f"{_render_url}/health" if _render_url else ""
)
SELF_PING_MIN_SEC = max(30, int(os.getenv("SELF_PING_MIN_SEC", "60")))
SELF_PING_MAX_SEC = max(
    SELF_PING_MIN_SEC, int(os.getenv("SELF_PING_MAX_SEC", "120"))
)
HERE = Path(__file__).resolve().parent
TIMEOUT = (3, 12)      # panel: fast connect, bounded read
TG_TIMEOUT = (2, 6)    # Telegram replies: quick connect, no long hang
REQ_RETRIES = 3        # transient network / 5xx retries
RETRY_BASE = 0.35      # exponential backoff + jitter

# Premium emoji IDs from the sample live-SMS card.
EMOJI_IDS = {
    "▶️": "5264919878082509254",  # LIVE SMS
    "✅": "5281025313952776387",  # client
    "🪐": "5990181988558969463",  # range
    "⭐️": "5391165622795132751",  # CLI
    "📞": "5436206493856707442",  # number
    "⏩": "5470017648936898423",  # content
    "💠": "5472188750610057261",  # time
}
_premium_broken = False

LOCAL_TZ = timezone(timedelta(hours=5, minutes=30))  # IST
_TAG_RE = re.compile(r"</?[A-Za-z!][^<>]*>")
_TITLE_RE = re.compile(r"""title\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")

_session: requests.Session | None = None
_tg_session = requests.Session()   # 1 connection reuse — delay kam

# Pure in-memory state. Koi file / cache / disk write nahi.
# (Sirf _seen; DM/subscription state ab bilkul nahi.)

# ---------------------------------------------------------------- panel ----
def _get_session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        _session = s
    return _session


def _login(sess: requests.Session) -> None:
    if not USERNAME or not PASSWORD:
        raise RuntimeError("PANEL_USERNAME / PANEL_PASSWORD .env me nahi hain")

    def once() -> None:
        resp = sess.post(
            BASE_URL + LOGIN_PATH,
            data={"username": USERNAME, "password": PASSWORD},
            timeout=TIMEOUT,
        )
        if resp.status_code == 429:
            try:
                wait = float(resp.headers.get("Retry-After", "5") or 5)
            except (TypeError, ValueError):
                wait = 5.0
            time.sleep(min(max(wait, 1.0), 30.0))
            raise RuntimeError("panel login 429 rate-limited")
        resp.raise_for_status()
        if 'action="/auth/login"' in resp.text:
            # Galat credentials par dobara-dobar login nahi — isse 429 milta hai.
            snippet = re.sub(r"\s+", " ", resp.text)[:180]
            log.warning(
                "panel login rejected: status=%s url=%s len=%d body=%s",
                resp.status_code,
                resp.url,
                len(resp.text),
                snippet,
            )
            raise _NoRetry("panel login fail: username/password check karein")

    _retry(once)


def _params(length: int) -> dict:
    """Browser ke SMS Report jaisa exact param set. Payout columns nahi."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    p = {
        "draw": 1,
        "start": 0,
        "length": length,
        "search[value]": "",
        "search[regex]": "false",
        "order[0][column]": 0,
        "order[0][dir]": "desc",
        "fsearch": "",
        "fdate": f"{day} 00:00:00",
        "fdate_to": f"{day} 23:59:59",
        "frange": "",
        "fnumber": "",
        "fcli": "",
        "fclient": "",
        "fsuperagent": "",
        "fagent": "",
        "flimit": "",
        "fsource": "",
        "group_by": "",
    }
    for i, col in enumerate(("time", "range", "number", "client", "cli", "sms")):
        p[f"columns[{i}][data]"] = col
        p[f"columns[{i}][name]"] = col
        p[f"columns[{i}][searchable]"] = "true"
        p[f"columns[{i}][orderable]"] = "true"
        p[f"columns[{i}][search][value]"] = ""
        p[f"columns[{i}][search][regex]"] = "false"
    return p


def _clean(value) -> str:
    s = str(value or "")
    if "<" not in s:
        return " ".join(s.split())
    return " ".join(html.unescape(_TAG_RE.sub(" ", s)).split())


def _content(raw) -> str:
    r = str(raw or "")
    if "<" not in r:
        return " ".join(r.split())
    m = _TITLE_RE.search(r)
    titled = " ".join(html.unescape(m.group(1) or m.group(2) or "").split()) if m else ""
    text = " ".join(html.unescape(_TAG_RE.sub(" ", r)).split())
    return titled if len(titled) > len(text) else text


def _parse(data) -> list[dict]:
    """Sirf safe fields. Payout keys kabhi read hi nahi ki jaati."""
    rows: list[dict] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        tm = _clean(
            raw.get("time")
            or raw.get("created_at")
            or raw.get("received_at")
            or raw.get("date")
            or ""
        )
        m = _DATE_RE.search(tm)
        if not m:
            continue
        number = _clean(raw.get("number") or raw.get("phone") or raw.get("msisdn") or "").split()
        sms = raw.get("sms")
        if sms is None:
            sms = raw.get("content") or raw.get("message") or raw.get("text") or ""
        rows.append(
            {
                "time": m.group(0).replace("T", " "),
                "range": _clean(raw.get("range") or raw.get("range_name") or ""),
                "number": number[0] if number else "",
                "client": _clean(raw.get("client") or raw.get("client_name") or ""),
                "cli": _clean(raw.get("cli") or raw.get("app") or raw.get("sender") or ""),
                "content": _content(sms),
            }
        )
    return rows


_cli_names_cache: list[str] = []
_cli_names_at = 0.0


def _panel_json(sess: requests.Session, path: str, params: dict | None = None) -> dict:
    """Panel JSON endpoint — session expire par ek hi baar re-login."""

    def once():
        resp = sess.get(
            BASE_URL + path,
            params=params,
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=TIMEOUT,
        )
        if (
            resp.status_code in (401, 403)
            or resp.status_code >= 500
            or "application/json" not in resp.headers.get("Content-Type", "")
        ):
            _login(sess)
            resp = sess.get(
                BASE_URL + path,
                params=params,
                headers={"X-Requested-With": "XMLHttpRequest"},
                timeout=TIMEOUT,
            )
        resp.raise_for_status()
        return resp.json()

    return _retry(once)


def _cli_names(sess: requests.Session) -> list[str]:
    """Live Feed se CLI names — memory me cache, disk par kuch nahi."""
    global _cli_names_cache, _cli_names_at
    now = time.time()
    if _cli_names_cache and now - _cli_names_at < CLI_LIST_REFRESH_SEC:
        return list(_cli_names_cache)
    payload = _panel_json(sess, LIVE_FEED_PATH, {"limit": 500})
    raw = payload.get("clis") if isinstance(payload, dict) else None
    names: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            name = _clean(item.get("cli")) if isinstance(item, dict) else _clean(item)
            if name and name not in names:
                names.append(name)
    if names:
        _cli_names_cache = names
        _cli_names_at = now
    return list(_cli_names_cache)


def _my_cli_names(sess: requests.Session) -> list[str]:
    """CLI name se stable shard — list order badle to bhi duplicate nahi."""
    names = _cli_names(sess)
    if not names or CLI_SHARD_COUNT <= 1:
        return names or [""]
    shard = [
        name
        for name in names
        if zlib.crc32(name.encode("utf-8")) % CLI_SHARD_COUNT == CLI_SHARD_INDEX
    ]
    return shard or [""]


def _fetch_rows_for(sess: requests.Session, cli: str = "") -> list[dict]:
    """SMS Report — fcli diya gaya to usi CLI ki rows, warna sab."""

    def once():
        params = _params(FETCH_LIMIT)
        if cli:
            params["fcli"] = cli
        resp = sess.get(
            BASE_URL + SMS_API_PATH,
            params=params,
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=TIMEOUT,
        )
        if (
            resp.status_code in (401, 403)
            or resp.status_code >= 500
            or "application/json" not in resp.headers.get("Content-Type", "")
        ):
            _login(sess)
            resp = sess.get(
                BASE_URL + SMS_API_PATH,
                params=params,
                headers={"X-Requested-With": "XMLHttpRequest"},
                timeout=TIMEOUT,
            )
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if data is None and isinstance(payload, dict):
            data = payload.get("rows") or payload.get("sms") or []
        if isinstance(data, dict):
            data = data.get("data") or data.get("rows") or []
        return _parse(data if isinstance(data, list) else [])

    return _retry(once)


def fetch_rows() -> list[dict]:
    """Sab CLI ki rows (local test / fallback)."""
    return _fetch_rows_for(_get_session(), "")


def iter_rows():
    """Sharded CLI search — nayi row milte hi yield, delay nahi."""
    sess = _get_session()
    if not CLI_SEARCH:
        yield from fetch_rows()
        return
    for index, cli in enumerate(_my_cli_names(sess)):
        if index:
            time.sleep(CLI_CHECK_SEC)
        for row in reversed(_fetch_rows_for(sess, cli)):
            yield row


# ---------------------------------------------------------------- format ----
def _local_time(ts: str) -> str:
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return ts
    s = dt.astimezone(LOCAL_TZ).strftime("%d %b • %I:%M %p")
    return re.sub(r"\b0(\d:\d\d)", r"\1", s)


def _row_utc(row: dict) -> datetime:
    try:
        return datetime.strptime(str(row.get("time", "")), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def emo(glyph: str, premium: bool = True) -> str:
    if not premium or not PREMIUM_EMOJI or _premium_broken:
        return glyph
    eid = EMOJI_IDS.get(glyph)
    return f'<tg-emoji emoji-id="{eid}">{glyph}</tg-emoji>' if eid else glyph


def format_row(row: dict, premium: bool = True) -> str:
    esc = lambda v: html.escape(str(v or "-"))
    lines = [f"{emo('▶️', premium)} <b>LIVE SMS</b>", ""]
    if row.get("client"):
        lines.append(f"{emo('✅', premium)} <code>{esc(row['client'])}</code>")
    if row.get("range"):
        lines.append(f"{emo('🪐', premium)} <code>{esc(row['range'])}</code>")
    if row.get("cli"):
        lines.append(f"{emo('⭐️', premium)} <code>{esc(row['cli'])}</code>")
    if row.get("number"):
        lines.append(f"{emo('📞', premium)} <code>{esc(row['number'])}</code>")
    content = " ".join(str(row.get("content") or "").split())
    if content:
        lines.append(f"{emo('⏩', premium)} {html.escape(content[:400])}")
    if row.get("time"):
        lines.append(f"{emo('💠', premium)} <code>{esc(_local_time(row['time']))}</code>")
    return "\n".join(lines)


# ---------------------------------------------------------------- telegram --
def _telegram(
    method: str, payload: dict | None = None, params: dict | None = None
) -> dict:
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"

    def once() -> dict:
        if payload is None:
            resp = _tg_session.get(url, params=params, timeout=TG_TIMEOUT)
        else:
            resp = _tg_session.post(url, json=payload, timeout=TG_TIMEOUT)
        if resp.status_code == 429:
            try:
                wait = float((resp.json().get("parameters") or {}).get("retry_after", 2))
            except Exception:
                wait = 2.0
            time.sleep(min(max(wait, 0.2), 8.0))
            raise RuntimeError(f"telegram {method} HTTP 429 rate-limited")
        if not resp.ok:
            msg = f"telegram {method} HTTP {resp.status_code}: {resp.text[:200]}"
            # 4xx permanent hai (emoji/parse/auth): yahan retry waste aur slow.
            if 400 <= resp.status_code < 500:
                raise _NoRetry(msg)
            raise RuntimeError(msg)
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"telegram {method}: {str(body)[:200]}")
        return body

    return _retry(once)


def card_text(row: dict, premium: bool = True) -> str:
    """Group card renderer — same premium emoji, fields, HTML, time format."""
    return format_row(row, premium=premium)


def _send(row: dict, premium: bool, chat_id: str | int = CHAT_ID) -> None:
    _telegram(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": card_text(row, premium=premium),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )


def send_sms(row: dict, chat_id: str | int = CHAT_ID) -> bool:
    global _premium_broken
    try:
        _send(row, True, chat_id)
        return True
    except Exception as exc:
        msg = str(exc).lower()
        if not any(w in msg for w in ("emoji", "invalid", "bad request")):
            log.warning("send failed to %s: %s", chat_id, exc)
            return False
        _premium_broken = True
        log.warning("premium emoji rejected (%s) — plain emoji se retry", exc)
        try:
            _send(row, False, chat_id)
            return True
        except Exception as exc2:
            log.warning("plain retry failed to %s: %s", chat_id, exc2)
            return False


# ------------------------------------------------------------------ watch ---
_seen: dict[str, float] = {}
_SEEN_TTL = 86400
_SEEN_MAX = 4000


def _key(row: dict) -> str:
    return "|".join(
        str(row.get(k, "")).strip() for k in ("time", "number", "cli", "content")
    )


def _remember(row: dict) -> bool:
    """True = nayi entry. Purani/duplicate = False."""
    now = time.time()
    key = _key(row)
    old = _seen.get(key)
    if old is not None and now - old < _SEEN_TTL:
        return False
    _seen[key] = now
    if len(_seen) > _SEEN_MAX:
        for k in list(_seen)[:300]:
            if now - _seen[k] >= _SEEN_TTL:
                _seen.pop(k, None)
        while len(_seen) > _SEEN_MAX:
            _seen.pop(next(iter(_seen)))
    return True


def watch() -> None:
    baseline = False
    while True:
        first_cycle = not baseline
        ignored = 0
        catchup: list[dict] = []
        cutoff = BOOT_UTC - timedelta(seconds=RESTART_CATCHUP_SEC)
        try:
            for row in iter_rows():
                if not _remember(row):
                    continue
                if first_cycle:
                    if RESTART_CATCHUP_SEC and _row_utc(row) >= cutoff:
                        catchup.append(row)
                    else:
                        ignored += 1
                    continue
                if send_sms(row):
                    log.info("group sent: %s | %s", row.get("client"), row.get("range"))
                time.sleep(SEND_DELAY_SEC)
            if first_cycle:
                baseline = True
                for row in catchup:
                    if send_sms(row):
                        log.info(
                            "restart catch-up sent: %s | %s",
                            row.get("client"), row.get("range"),
                        )
                    time.sleep(SEND_DELAY_SEC)
                log.info(
                    "baseline ready: %d old rows ignored, %d restart catch-up",
                    ignored, len(catchup),
                )
        except Exception:
            log.exception("poll error — retry in 2s")
            time.sleep(2)
        time.sleep(POLL_SEC)


def _health_server() -> None:
    """Render ke liye /health endpoint — 200 ok for any GET."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    HTTPServer(("0.0.0.0", PORT), Health).serve_forever()


def _self_ping() -> None:
    """Boot par turant, phir har 1-2 min: Render idle spin-down reset hota hai."""
    sess = requests.Session()
    while True:
        if SELF_PING_URL:
            try:
                sess.get(SELF_PING_URL, timeout=(2, 6))
            except Exception as exc:
                log.warning("self-ping fail: %s", exc)
        time.sleep(random.uniform(SELF_PING_MIN_SEC, SELF_PING_MAX_SEC))


def main() -> None:
    threading.Thread(target=_health_server, name="health", daemon=True).start()
    if SELF_PING_URL:
        threading.Thread(target=_self_ping, name="self-ping", daemon=True).start()
    for name, value in (
        ("TELEGRAM_BOT_TOKEN", TOKEN),
        ("SMS_LIVE_CHAT", CHAT_ID),
        ("PANEL_USERNAME", USERNAME),
        ("PANEL_PASSWORD", PASSWORD),
    ):
        if not value:
            raise RuntimeError(f".env me {name} missing")
    try:
        int(CHAT_ID)
    except ValueError:
        raise RuntimeError("chat id numeric hona chahiye (-100...)")
    me = _telegram("getMe")
    log.info(
        "online as @%s -> group %s (poll %ss, payout: never, dm=off, panel=%s, cli-shard=%d/%d, cli-check=%ss)",
        me["result"].get("username", "?"),
        CHAT_ID,
        POLL_SEC,
        USERNAME or "?",
        CLI_SHARD_INDEX + 1 if CLI_SHARD_COUNT > 1 else 0,
        CLI_SHARD_COUNT,
        CLI_CHECK_SEC,
    )
    try:
        sess = _get_session()
        _login(sess)  # 1 baar login, usi session par sab kuch
        log.info("panel login once OK (same session reused)")
        if CLI_SEARCH:
            names = _my_cli_names(sess)
            log.info("CLI search ready: %d CLI in this shard", len([n for n in names if n]))
    except Exception:
        log.exception("startup panel login fail — zaroorat par re-login hoga")
    watch()


if __name__ == "__main__":
    main()
