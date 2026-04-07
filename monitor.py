#!/usr/bin/env python3
"""
IMAX 70mm Showtime Monitor
Watches AMC Lincoln Square 13 for Dune Part Three and The Odyssey in IMAX 70mm.
Sends a desktop notification, email alert, and logs with timestamp on any change.

Environment variables (required for email):
  GMAIL_USER         — your Gmail address used to send (e.g. you@gmail.com)
  GMAIL_APP_PASSWORD — Gmail App Password (not your login password)
  ALERT_EMAIL        — address to send alerts to (defaults to henry10greene@gmail.com)
"""

import asyncio
import json
import logging
import os
import smtplib
import subprocess
import sys
from email.mime.text import MIMEText
from pathlib import Path

from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────────────────────

THEATER_SLUG = "new-york/amc-lincoln-square-13"
BASE_URL = "https://www.amctheatres.com"
THEATER_SHOWTIMES_URL = (
    f"{BASE_URL}/movie-theatres/{THEATER_SLUG}"
    "/showtimes/all-movies/today/all-auditoriums"
)

MOVIES = ["dune part three", "the odyssey"]
FORMATS = ["imax 70mm", "imax laser at amc", "imax at amc"]  # broaden if needed

CHECK_INTERVAL = 60  # seconds

DIR = Path(__file__).parent
LOG_FILE = DIR / "imax_monitor.log"
STATE_FILE = DIR / "imax_state.json"

GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "henry10greene@gmail.com")

# ── Logging ───────────────────────────────────────────────────────────────────

handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
try:
    handlers.append(logging.FileHandler(LOG_FILE))
except OSError:
    pass  # read-only filesystem on some cloud envs — stdout only

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=handlers,
)
log = logging.getLogger(__name__)

# ── Alerts ────────────────────────────────────────────────────────────────────

def notify_desktop(title: str, body: str) -> None:
    """Fire notify-send — silently skipped if unavailable (e.g. on Render)."""
    try:
        subprocess.run(
            ["notify-send", "--urgency=critical", "--expire-time=10000", title, body],
            check=True,
            capture_output=True,
        )
    except Exception:
        pass  # not available in headless/cloud environments


def notify_email(subject: str, body: str) -> None:
    """Send a Gmail alert. Skipped if credentials are not configured."""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        log.warning("Email credentials not set — skipping email alert.")
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = GMAIL_USER
        msg["To"] = ALERT_EMAIL
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

# ── State persistence ─────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}

def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ── Scraping ──────────────────────────────────────────────────────────────────

def is_target_movie(title: str) -> bool:
    t = title.lower().strip()
    return any(m in t for m in MOVIES)

def is_imax_70mm(label: str) -> bool:
    l = label.lower()
    return "70mm" in l or any(f in l for f in FORMATS)

async def fetch_showtimes(page) -> dict[str, list[str]]:
    """
    Returns a dict mapping movie title → list of showtime strings found
    that match our target movies and IMAX 70mm format.
    """
    results: dict[str, list[str]] = {}

    await page.goto(THEATER_SHOWTIMES_URL, wait_until="networkidle", timeout=60_000)

    # Give any lazy-loaded content a moment
    await page.wait_for_timeout(3000)

    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")

    # AMC renders movie cards; each has a title and format badges + showtime buttons.
    # We look for any element containing our movie names, then walk up to find times.
    # This is intentionally broad so it survives minor HTML tweaks.

    found_any_target = False

    # Strategy 1: look for movie title headings then find sibling/child showtime data
    for heading in soup.find_all(["h2", "h3", "h4", "span", "a"]):
        text = heading.get_text(strip=True)
        if not is_target_movie(text):
            continue

        found_any_target = True
        movie_title = text

        # Walk up to a container that likely holds format + showtimes
        container = heading
        for _ in range(6):
            container = container.parent
            if container is None:
                break
            container_text = container.get_text(" ", strip=True).lower()
            if "70mm" in container_text or "imax" in container_text:
                break

        if container is None:
            continue

        # Collect showtime buttons / time strings within this container
        times: list[str] = []
        for el in container.find_all(["a", "button", "span"]):
            el_text = el.get_text(strip=True)
            # Showtime buttons look like "7:00pm", "10:30am", etc.
            if len(el_text) <= 10 and (
                "am" in el_text.lower() or "pm" in el_text.lower()
            ):
                times.append(el_text)

        # Check format label within container
        container_str = container.get_text(" ", strip=True)
        if is_imax_70mm(container_str) or not times:
            # Store even if empty — presence of the card matters
            key = movie_title
            if key not in results:
                results[key] = []
            results[key].extend(times)

    # Strategy 2: broader text scan if nothing found yet
    if not found_any_target:
        body_text = soup.get_text(" ", strip=True)
        for movie in MOVIES:
            if movie in body_text.lower():
                results[movie.title()] = results.get(movie.title(), [])

    # Deduplicate times
    return {k: sorted(set(v)) for k, v in results.items()}

# ── Main loop ─────────────────────────────────────────────────────────────────

async def run() -> None:
    log.info("=" * 60)
    log.info("IMAX 70mm monitor starting up")
    log.info(f"Watching: {', '.join(m.title() for m in MOVIES)}")
    log.info(f"Theater:  AMC Lincoln Square 13")
    log.info(f"Interval: {CHECK_INTERVAL}s")
    log.info(f"Log file: {LOG_FILE}")
    log.info(f"Email alerts → {ALERT_EMAIL}" if GMAIL_USER else "Email alerts → DISABLED (set GMAIL_USER + GMAIL_APP_PASSWORD)")
    log.info("=" * 60)

    state = load_state()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        while True:
            try:
                log.info("Checking AMC Lincoln Square 13 showtimes...")
                showtimes = await fetch_showtimes(page)

                if showtimes:
                    log.info(f"Found target movies on page: {list(showtimes.keys())}")
                else:
                    log.info("No target movies found on page yet.")

                # Compare to last known state
                if showtimes != state:
                    changes: list[str] = []

                    # New movies appeared
                    for title, times in showtimes.items():
                        if title not in state:
                            changes.append(
                                f"NEW: {title} appeared"
                                + (f" — {', '.join(times)}" if times else " (no times yet)")
                            )
                        elif times != state[title]:
                            added = sorted(set(times) - set(state.get(title, [])))
                            removed = sorted(set(state.get(title, [])) - set(times))
                            if added:
                                changes.append(f"{title}: added times {', '.join(added)}")
                            if removed:
                                changes.append(f"{title}: removed times {', '.join(removed)}")

                    # Movies disappeared
                    for title in state:
                        if title not in showtimes:
                            changes.append(f"{title}: disappeared from page")

                    if changes:
                        summary = "\n".join(changes)
                        log.info(f"CHANGE DETECTED:\n{summary}")
                        alert("IMAX 70mm Alert — AMC Lincoln Square 13", summary)

                    state = showtimes
                    save_state(state)
                else:
                    log.info("No change.")

            except Exception as e:
                log.error(f"Error during check: {e}")

            await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run())
