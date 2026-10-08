#!/usr/bin/env python3
"""Watch GTT Porta Nuova (UFirst) for new appointment slots; alert on Telegram.

Single run per call; schedule it every 5 minutes (cron example below).

Env vars for Telegram alerts:
  TELEGRAM_TOKEN       token from @BotFather
  TELEGRAM_CHAT_ID     your chat id
Optional env vars:
  SERVICE_ID           default: Trasporto Pubblico - Porta Nuova
  BEFORE_DATE          YYYY-MM-DD; only alert for slots on or before this day
  MIN_DATE             YYYY-MM-DD; ignore slots before this day (alerts and status)
  HEARTBEAT            set to 1 to send a silent status message to Telegram
  HEARTBEAT_EVERY      minutes between status messages (default 15)
Flags:
  --debug              print HTTP status/body details (use for the first run)

cron:  */5 * * * * cd /path/to/gtt-monitor && ./venv/bin/python ufirst_watcher.py

The script asks UFirst's own server for the slot list (no browser needed).
It alerts when slots appear that were not in the previous check: a new day
being released, or a cancellation freeing an earlier slot. It never books.
"""
import os
import sys
import time
import uuid
from pathlib import Path

import requests

FIREBASE_KEY = os.environ.get("FIREBASE_KEY", "AIzaSyCaXozpZZNpKYJd6GWOaTn-D-IGu6YPdBI")
HIVE = "https://hive.production.ufirst.link"
SERVICE_ID = os.environ.get("SERVICE_ID", "QQSP000000102-102-S_trasporto-pubblico")
BEFORE_DATE = os.environ.get("BEFORE_DATE", "")
MIN_DATE = os.environ.get("MIN_DATE", "")
HEARTBEAT = os.environ.get("HEARTBEAT", "") == "1"
HEARTBEAT_EVERY = int(os.environ.get("HEARTBEAT_EVERY", "15"))
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
BOOK_URL = "https://www.ufirst.com/book/it/organizations/172/points/QQSP000000102"
STATE_FILE = Path(__file__).with_name("ufirst_state.json")
DEBUG = "--debug" in sys.argv

BROWSER_HEADERS = {
    "Origin": "https://www.ufirst.com",
    "Referer": "https://www.ufirst.com/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
}


# ---------- clock (always Turin time, whatever the machine's timezone is) ----------
def now_hm():
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Rome")).strftime("%H:%M")
    except Exception:
        return time.strftime("%H:%M")  # fallback: machine's local time


# ---------- state ----------
def load_state():
    import json
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(st):
    import json
    STATE_FILE.write_text(json.dumps(st))
    try:
        STATE_FILE.chmod(0o600)
    except Exception:
        pass


# ---------- telegram ----------
def telegram(text, silent=False):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("telegram notification skipped: TELEGRAM_TOKEN or TELEGRAM_CHAT_ID is not set", file=sys.stderr)
        return False
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": "true"}
    if silent:
        data["disable_notification"] = "true"  # delivered without a sound/vibration
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=data, timeout=15,
        )
        r.raise_for_status()
    except Exception as e:
        print("telegram notification failed:", e, file=sys.stderr)
        return False
    return True


# ---------- anonymous Firebase login (same as the web app does) ----------
def _sign_up(s):
    r = s.post(
        f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={FIREBASE_KEY}",
        json={"returnSecureToken": True}, headers=BROWSER_HEADERS, timeout=20,
    )
    r.raise_for_status()
    d = r.json()
    return d["idToken"], d["refreshToken"], int(d.get("expiresIn", 3600))


def _refresh(s, refresh_token):
    r = s.post(
        f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_KEY}",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        headers=BROWSER_HEADERS, timeout=20,
    )
    r.raise_for_status()
    d = r.json()
    return d["id_token"], d["refresh_token"], int(d.get("expires_in", 3600))


def get_token(s, st):
    """Reuse the saved anonymous account; only sign up again if refresh fails."""
    now = time.time()
    if st.get("id_token") and st.get("id_expiry", 0) - now > 120:
        return st["id_token"]
    try:
        if not st.get("refresh_token"):
            raise KeyError("no refresh token")
        tok, rt, exp = _refresh(s, st["refresh_token"])
    except Exception:
        tok, rt, exp = _sign_up(s)
    st.update(id_token=tok, refresh_token=rt, id_expiry=now + exp)
    return tok


# ---------- UFirst JSON-RPC ----------
def rpc(s, st, method, params):
    token = get_token(s, st)
    url = f"{HIVE}/contento_secure_rpc"
    body = {"id": 1, "jsonrpc": "2.0", "method": method, "params": params}
    schemes = [st.get("auth_scheme", "Bearer"), "Bearer", ""]
    tried = []
    for scheme in schemes:
        if scheme in tried:
            continue
        tried.append(scheme)
        headers = {
            "Content-Type": "application/json",
            "Accept-Language": "en",
            "X-ClientID": st["client_id"],
            "Authorization": f"{scheme} {token}".strip(),
            **BROWSER_HEADERS,
        }
        r = s.post(url, json=body, headers=headers, timeout=20)
        if DEBUG:
            print(f"[{method}] HTTP {r.status_code} scheme={scheme!r}: {r.text[:300]}")
        if r.status_code in (401, 403):
            continue
        r.raise_for_status()
        d = r.json()
        if "error" in d:
            raise RuntimeError(f"RPC error: {d['error']}")
        st["auth_scheme"] = scheme
        return d["result"]
    raise RuntimeError("authorization rejected (HTTP 401/403)")


def fetch_slots(s, st):
    res = rpc(s, st, "Contento.GetServiceResourcesAvailability", {"serviceID": SERVICE_ID})
    slots = set()
    for r in res.get("resourcesAvailability", []):
        for t in r.get("timeslots", []):
            slots.add(t["startTimeRFC3339"])
    if MIN_DATE:
        slots = {t for t in slots if t[:10] >= MIN_DATE}
    return slots


def heartbeat_due(st):
    # 60 s tolerance so a 5-minute cron still lands on the 15-minute mark
    return HEARTBEAT and time.time() - st.get("last_heartbeat", 0) >= HEARTBEAT_EVERY * 60 - 60


# ---------- messaging ----------
def summarize(slots):
    by_day = {}
    for t in sorted(slots):
        by_day.setdefault(t[:10], []).append(t[11:16])
    lines = []
    for day, times in sorted(by_day.items())[:10]:
        extra = " …" if len(times) > 8 else ""
        lines.append(f"{day}: {', '.join(times[:8])}{extra}")
    return "\n".join(lines)


def run(s, st):
    slots = fetch_slots(s, st)
    prev = set(st.get("slots", []))
    first = not st.get("initialized")

    new = sorted(slots - prev)
    if BEFORE_DATE:
        new = [t for t in new if t[:10] <= BEFORE_DATE]

    if first:
        earliest = min(slots) if slots else "none right now"
        telegram(f"✅ GTT Porta Nuova watcher is running.\n{len(slots)} slots now; earliest: {earliest}\n{BOOK_URL}")
    elif new:
        telegram(f"🚨 New GTT Porta Nuova slots ({len(new)}):\n{summarize(new)}\n\nBook: {BOOK_URL}")

    st.update(slots=sorted(slots), initialized=True)
    print(f"{len(slots)} slots, {len(new)} new" + (" (first run)" if first else ""))

    if first:
        st["last_heartbeat"] = time.time()
    elif heartbeat_due(st):
        scope = f" (from {MIN_DATE})" if MIN_DATE else ""
        if slots:
            earliest = min(slots)[:16].replace("T", " ")
            msg = f"🕒 {now_hm()} free slots{scope}: {len(slots)}, earliest {earliest}"
        else:
            msg = f"🕒 {now_hm()} no free slots at all{scope}"
        telegram(msg, silent=True)
        st["last_heartbeat"] = time.time()


def main():
    st = load_state()
    st.setdefault("client_id", str(uuid.uuid4()))
    s = requests.Session()
    try:
        run(s, st)
        st["errors"] = 0
    except Exception as e:
        st["errors"] = st.get("errors", 0) + 1
        print("check failed:", e, file=sys.stderr)
        if heartbeat_due(st):
            try:
                telegram(f"❌ {now_hm()} check FAILED: {e}", silent=True)
                st["last_heartbeat"] = time.time()
            except Exception:
                pass
        if st["errors"] == 12:  # ~1 hour of failures at a 5-minute interval
            try:
                telegram(f"⚠️ GTT watcher failing for ~1 hour: {e}")
            except Exception:
                pass
        save_state(st)
        sys.exit(1)
    save_state(st)


if __name__ == "__main__":
    main()
