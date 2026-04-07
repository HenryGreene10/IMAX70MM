#!/usr/bin/env python3
"""
IMAX 70mm Showtime Monitor — AMC Lincoln Square 13
Primary:  AMC API  (https://api.amctheatres.com/v2)
Fallback: Fandango theater page scrape (used until AMC key activates)
Switches automatically — no code change needed when the key goes live.

Required env vars:
  AMC_API_KEY        — your AMC vendor key
  GMAIL_USER         — Gmail address used to send alerts
  GMAIL_APP_PASSWORD — Gmail App Password
  ALERT_EMAIL        — recipient (defaults to henry10greene@gmail.com)
"""

import json
import logging
import os
import re
import smtplib
import subprocess
import sys
import time
from datetime import date, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────────────────────

AMC_API_KEY     = os.environ["AMC_API_KEY"]
GMAIL_USER      = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
ALERT_EMAIL     = os.environ.get("ALERT_EMAIL", "henry10greene@gmail.com")

THEATRE_ID      = 164          # AMC Lincoln Square 13
DAYS_AHEAD      = 30
CHECK_INTERVAL  = 60           # seconds

TARGET_MOVIES   = ["dune", "odyssey"]
TARGET_FORMAT   = "imax 70mm"

API_BASE        = "https://api.amctheatres.com/v2"
AMC_HEADERS     = {"X-AMC-Vendor-Key": AMC_API_KEY}

FANDANGO_URL    = "https://www.fandango.com/amc-lincoln-square-13-aabqi/theater-page"
FANDANGO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

DIR             = Path(__file__).parent
LOG_FILE        = DIR / "imax_monitor.log"
STATE_FILE      = DIR / "imax_state.json"

# ── Logging ───────────────────────────────────────────────────────────────────

handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
try:
    handlers.append(logging.FileHandler(LOG_FILE))
except OSError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=handlers,
)
log = logging.getLogger(__name__)

# ── Alerts ────────────────────────────────────────────────────────────────────

def notify_desktop(title: str, body: str) -> None:
    try:
        subprocess.run(
            ["notify-send", "--urgency=critical", "--expire-time=10000", title, body],
            check=True, capture_output=True,
        )
    except Exception:
        pass


def notify_email(subject: str, body: str) -> None:
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        log.warning("Email credentials not configured — skipping email alert.")
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"]    = GMAIL_USER
        msg["To"]      = ALERT_EMAIL
        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            smtp.sendmail(GMAIL_USER, ALERT_EMAIL, msg.as_string())
        log.info(f"Email alert sent to {ALERT_EMAIL}")
    except Exception as e:
        log.error(f"Failed to send email: {e}")


def alert(title: str, body: str) -> None:
    notify_desktop(title, body)
    notify_email(f"[IMAX Alert] {title}", body)

# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError:
        pass

# ── Helpers ───────────────────────────────────────────────────────────────────

class AMCKeyInactive(Exception):
    """Raised when the AMC API returns 401/403 — key not yet active."""


def is_target_movie(name: str) -> bool:
    return any(m in name.lower() for m in TARGET_MOVIES)


def is_target_format(attrs: list[str]) -> bool:
    return TARGET_FORMAT in " ".join(attrs).lower()


# ── AMC API (primary) ─────────────────────────────────────────────────────────

def _fetch_amc_date(day: date) -> list[dict]:
    url = f"{API_BASE}/theatres/{THEATRE_ID}/showtimes/{day.isoformat()}"
    resp = requests.get(url, headers=AMC_HEADERS, timeout=15)
    if resp.status_code in (401, 403):
        raise AMCKeyInactive(f"AMC API returned {resp.status_code} — key not active yet")
    resp.raise_for_status()

    hits = []
    for st in resp.json().get("_embedded", {}).get("showtimes", []):
        movie_name = st.get("movieName", "") or st.get("name", "")
        attributes = st.get("attributes", [])
        if is_target_movie(movie_name) and is_target_format(attributes):
            hits.append({
                "movie":    movie_name,
                "date":     day.isoformat(),
                "time":     st.get("showDateTime", st.get("showDateTimeUtc", "")),
                "format":   ", ".join(attributes),
                "purchase": st.get("purchaseUrl", ""),
                "source":   "amc-api",
            })
    return hits


def fetch_all_showtimes_amc() -> dict[str, list[dict]]:
    """Check the next DAYS_AHEAD days via the AMC API."""
    today = date.today()
    results: dict[str, list[dict]] = {}
    for offset in range(DAYS_AHEAD):
        day = today + timedelta(days=offset)
        hits = _fetch_amc_date(day)   # raises AMCKeyInactive on 401/403
        if hits:
            results[day.isoformat()] = hits
    return results


# ── Fandango scrape (fallback) ────────────────────────────────────────────────

def fetch_all_showtimes_fandango() -> dict[str, list[dict]]:
    """
    Scrape the Fandango Lincoln Square 13 theater page.
    Fandango renders showtime data into the HTML for the next several days,
    grouped by movie then by date. We walk the DOM looking for our target
    movies and IMAX 70mm format rows.
    """
    resp = requests.get(FANDANGO_URL, headers=FANDANGO_HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    results: dict[str, list[dict]] = {}
    today = date.today()

    # Fandango groups showtimes under a movie section. Each section has:
    #   - a heading/link with the movie title
    #   - one or more date tabs, each containing format rows with time buttons
    #
    # We walk every element that contains our movie names, climb to the
    # enclosing movie block, then extract date+format+time triples.

    # Map short Fandango date labels → ISO date strings.
    def resolve_date(label: str) -> str | None:
        label = label.strip().lower()
        if label in ("today",):
            return today.isoformat()
        if label in ("tomorrow",):
            return (today + timedelta(days=1)).isoformat()
        # Try "Mon Apr 7", "April 7", "4/7" etc.
        for fmt in ("%a %b %d", "%B %d", "%m/%d", "%b %d"):
            try:
                parsed = date.today().replace(
                    **dict(zip(
                        ["month", "day"],
                        [int(x) for x in re.findall(r"\d+", label)][:2]
                    ))
                )
                # Roll into next year if the date already passed this year
                if parsed < today:
                    parsed = parsed.replace(year=today.year + 1)
                return parsed.isoformat()
            except Exception:
                continue
        return None

    # Find every movie container on the page
    for movie_block in soup.find_all(True, recursive=True):
        block_text = movie_block.get_text(" ", strip=True)

        # Only consider blocks that mention our movies AND IMAX 70mm
        if not is_target_movie(block_text):
            continue
        if TARGET_FORMAT not in block_text.lower():
            continue
        # Skip tiny fragments (must be a real container)
        if len(block_text) < 50:
            continue

        movie_title = ""
        for tag in movie_block.find_all(["h2", "h3", "h4", "a"]):
            t = tag.get_text(strip=True)
            if is_target_movie(t) and len(t) < 60:
                movie_title = t
                break
        if not movie_title:
            continue

        # Walk child elements looking for date labels and time buttons
        current_date_str: str | None = None
        for el in movie_block.descendants:
            if not hasattr(el, "get_text"):
                continue
            text = el.get_text(strip=True)
            if not text:
                continue

            # Date label candidates: short text with day/month info
            if len(text) < 20 and re.search(r"(today|tomorrow|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|\d{1,2}/\d{1,2})", text, re.I):
                resolved = resolve_date(text)
                if resolved:
                    current_date_str = resolved

            # IMAX 70mm format label resets our date context check
            if TARGET_FORMAT in text.lower():
                pass  # keep current_date_str, times follow below

            # Time buttons: "7:00pm", "10:30am"
            if current_date_str and re.fullmatch(r"\d{1,2}:\d{2}[ap]m", text, re.I):
                day_results = results.setdefault(current_date_str, [])
                entry = {
                    "movie":    movie_title,
                    "date":     current_date_str,
                    "time":     text,
                    "format":   "IMAX 70mm",
                    "purchase": "",
                    "source":   "fandango",
                }
                if entry not in day_results:
                    day_results.append(entry)

    return results


# ── Unified fetch ─────────────────────────────────────────────────────────────

def fetch_all_showtimes() -> tuple[dict[str, list[dict]], str]:
    """
    Try AMC API first. On 401/403, fall back to Fandango.
    Returns (results, source) where source is 'amc-api' or 'fandango'.
    """
    try:
        results = fetch_all_showtimes_amc()
        return results, "amc-api"
    except AMCKeyInactive as e:
        log.warning(f"{e} — falling back to Fandango scrape")
        results = fetch_all_showtimes_fandango()
        return results, "fandango"

# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=" * 60)
    log.info("IMAX 70mm monitor starting up")
    log.info(f"Theatre:  AMC Lincoln Square 13 (ID {THEATRE_ID})")
    log.info(f"Watching: Dune, The Odyssey — IMAX 70mm")
    log.info(f"Window:   next {DAYS_AHEAD} days")
    log.info(f"Interval: {CHECK_INTERVAL}s")
    log.info(f"Email:    {ALERT_EMAIL}" if GMAIL_USER else "Email:    DISABLED")
    log.info("=" * 60)

    state = load_state()

    while True:
        log.info(f"Checking next {DAYS_AHEAD} days...")
        try:
            current, source = fetch_all_showtimes()
            log.info(f"Source: {source}")

            if current != state:
                changes: list[str] = []

                for day, hits in current.items():
                    prev_hits = state.get(day, [])
                    new_hits = [h for h in hits if h not in prev_hits]
                    for h in new_hits:
                        line = f"NEW: {h['movie']} — {h['date']} {h['time']} [{h['format']}]"
                        if h["purchase"]:
                            line += f"\nBuy: {h['purchase']}"
                        changes.append(line)

                for day in state:
                    if day not in current:
                        for h in state[day]:
                            changes.append(f"REMOVED: {h['movie']} — {day} {h['time']}")

                if changes:
                    summary = "\n\n".join(changes)
                    log.info(f"CHANGE DETECTED:\n{summary}")
                    alert("IMAX 70mm Alert — AMC Lincoln Square 13", summary)
                else:
                    log.info("State changed (no net new showtimes).")

                state = current
                save_state(state)
            else:
                found = sum(len(v) for v in current.values())
                log.info(f"No change. ({found} target showtimes on record)")

        except Exception as e:
            log.error(f"Unexpected error: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
