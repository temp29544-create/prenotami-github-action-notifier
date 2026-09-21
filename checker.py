#!/usr/bin/env python3
"""
PrenotaMi Schengen Visa Slot Checker

Monitors the Italian consulate's PrenotaMi appointment system for available
Schengen visa slots. When a slot is found, it sends a notification email
so you can book it yourself — it does not touch the booking form.
"""

import os
import sys
import subprocess
import logging
import time
from datetime import datetime
from pathlib import Path

# --- Configuration ---
def load_env():
    """Load .env file if it exists."""
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

load_env()

EMAIL = os.environ.get("PRENOTAMI_EMAIL", "")
PASSWORD = os.environ.get("PRENOTAMI_PASSWORD", "")
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", EMAIL)
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))
NOTIFY_COOLDOWN_SECONDS = int(os.environ.get("NOTIFY_COOLDOWN", "1800"))
NOTIFY_METHOD = os.environ.get("NOTIFY_METHOD", "macos_mail")

LOG_DIR = Path(__file__).parent / "logs"
COOLDOWN_FILE = Path(__file__).parent / ".last_notified"
BOOKED_FILE = Path(__file__).parent / ".booked"

LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "checker.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("prenotami")

NOTIFICATION_LOG = LOG_DIR / "notifications.log"


def send_email_notification(subject: str, body: str):
    """Send notification via macOS alert + log file + say command."""
    clean_body = body.replace("\\\\n", "\n").replace("\\n", "\n")

    # 1. Write to notification log file (always works)
    try:
        with open(NOTIFICATION_LOG, "a") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"TIME: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"SUBJECT: {subject}\n")
            f.write(f"BODY:\n{clean_body}\n")
            f.write(f"{'='*60}\n")
        log.info(f"Notification logged to {NOTIFICATION_LOG}")
    except Exception as e:
        log.error(f"Failed to write notification log: {e}")

    # 2. macOS display notification
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{subject}" with title "PrenotaMi Alert" sound name "Glass"'],
            capture_output=True, timeout=10
        )
        log.info("macOS notification sent")
    except Exception as e:
        log.warning(f"macOS notification failed: {e}")

    # 3. Say it aloud so user hears it
    try:
        subprocess.Popen(["say", "PrenotaMi slot detected! Check the booking!"])
    except Exception:
        pass

    # 4. Try Gmail SMTP as best-effort (may fail)
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if gmail_password:
        try:
            import smtplib
            from email.mime.text import MIMEText
            msg = MIMEText(clean_body)
            msg["Subject"] = subject
            msg["From"] = EMAIL
            msg["To"] = NOTIFY_EMAIL
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
                server.login(EMAIL, gmail_password)
                server.sendmail(EMAIL, [NOTIFY_EMAIL], msg.as_string())
            log.info(f"Email sent to {NOTIFY_EMAIL} via Gmail SMTP")
        except Exception as e:
            log.warning(f"Gmail SMTP failed (non-critical): {e}")
    else:
        log.info("GMAIL_APP_PASSWORD not set — skipping email, using log + macOS notify")


def should_notify() -> bool:
    if not COOLDOWN_FILE.exists():
        return True
    try:
        last_notified = float(COOLDOWN_FILE.read_text().strip())
        return (time.time() - last_notified) > NOTIFY_COOLDOWN_SECONDS
    except (ValueError, OSError):
        return True


def mark_notified():
    COOLDOWN_FILE.write_text(str(time.time()))


def is_already_booked() -> bool:
    """Manual pause switch: create the .booked file yourself once you've booked
    (e.g. `touch .booked`) to stop further checks. Delete it to resume."""
    return BOOKED_FILE.exists()


ALL_BOOKED_INDICATORS = [
    "All appointments for this service are currently booked",
    "tutti gli appuntamenti",
    "currently booked",
    "attualmente esauriti",
    "posti disponibili per il servizio scelto sono esauriti",
    "elevata richiesta",
    "sono esauriti",
]


def check_page_for_all_booked(page) -> bool:
    """Check if the current page shows an 'all booked' message."""
    try:
        content = page.content()
        return any(ind in content for ind in ALL_BOOKED_INDICATORS)
    except:
        return False



def check_for_slots():
    """Log into PrenotaMi, check for Schengen visa slots, and email a notification if any are open."""
    from playwright.sync_api import sync_playwright

    if is_already_booked():
        log.info("✅ Already booked! Skipping check. Delete .booked file to re-enable.")
        return

    if not EMAIL or not PASSWORD:
        log.error("PRENOTAMI_EMAIL and PRENOTAMI_PASSWORD must be set.")
        sys.exit(1)

    log.info("Starting slot check...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()
        
        # Handle Facebook password reuse and any other JS alerts automatically
        page.on("dialog", lambda dialog: dialog.accept())

        try:
            # Step 1: Navigate to PrenotaMi
            log.info("Navigating to PrenotaMi...")
            page.goto("https://prenotami.esteri.it/", timeout=60000)
            page.wait_for_load_state("networkidle", timeout=45000)
            time.sleep(2)

            # Step 2: Click login
            log.info("Clicking login...")
            for sel in ["a:has-text('EFFETTUARE IL LOGIN')", "a:has-text('LOG IN')",
                        "a:has-text('Log in')", "a[href*='Login']"]:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=2000):
                        el.click()
                        break
                except:
                    continue

            page.wait_for_load_state("networkidle", timeout=45000)
            time.sleep(3)

            # Step 3: Login
            log.info("Logging in...")
            for sel in ["input#UserName", "input[name='UserName']", "input[type='text']"]:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=3000):
                        el.fill(EMAIL)
                        break
                except:
                    continue

            for sel in ["input#Password", "input[name='Password']", "input[type='password']"]:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=3000):
                        el.fill(PASSWORD)
                        break
                except:
                    continue

            for sel in ["button:has-text('Next')", "button:has-text('Sign in')", "button[type='submit']"]:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=2000):
                        el.click()
                        break
                except:
                    continue

            page.wait_for_load_state("networkidle", timeout=45000)
            time.sleep(5)

            page_text = page.content().lower()
            if "login failure" in page_text or "login failed" in page_text:
                log.error("Login failed!")
                page.screenshot(path=str(LOG_DIR / "login_failed.png"))
                return

            log.info("Login successful!")

            # Step 4: Navigate to Services
            log.info("Navigating to services...")
            page.goto("https://prenotami.esteri.it/Services", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)
            time.sleep(3)

            # Step 5: Click PRENOTA for Schengen visa
            log.info("Clicking Schengen visa PRENOTA...")
            schengen_clicked = page.evaluate("""() => {
                const rows = document.querySelectorAll('tr');
                for (const row of rows) {
                    const text = row.textContent.toLowerCase();
                    if (text.includes('schengen')) {
                        const allLinks = row.querySelectorAll('a, button');
                        for (const l of allLinks) {
                            const t = l.textContent.trim().toUpperCase();
                            if (t.includes('PRENOTA') || t.includes('BOOK')) {
                                l.click();
                                return true;
                            }
                        }
                    }
                }
                return false;
            }""")

            if not schengen_clicked:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(2)
                schengen_clicked = page.evaluate("""() => {
                    const rows = document.querySelectorAll('tr');
                    for (const row of rows) {
                        const text = row.textContent.toLowerCase();
                        if (text.includes('schengen')) {
                            const allLinks = row.querySelectorAll('a, button');
                            for (const l of allLinks) {
                                const t = l.textContent.trim().toUpperCase();
                                if (t.includes('PRENOTA') || t.includes('BOOK')) {
                                    l.click();
                                    return true;
                                }
                            }
                        }
                    }
                    return false;
                }""")

            if not schengen_clicked:
                log.warning("No Schengen PRENOTA button found")
                page.screenshot(path=str(LOG_DIR / "no_schengen.png"))
                return

            log.info("Clicked PRENOTA for Schengen visa")
            # Wait LONGER for any popup to fully render (was 3s, now 6s)
            time.sleep(6)

            # Step 6: Check result — do TWO checks with a gap
            page_content = page.content()
            page.screenshot(path=str(LOG_DIR / "after_prenota.png"))
            is_all_booked = any(ind in page_content for ind in ALL_BOOKED_INDICATORS)

            # If not detected yet, wait a bit more and check again
            if not is_all_booked:
                time.sleep(3)
                page_content2 = page.content()
                is_all_booked = any(ind in page_content2 for ind in ALL_BOOKED_INDICATORS)

            if is_all_booked:
                log.info("❌ No slots available - all booked.")
                try:
                    ok_btn = page.locator("button:has-text('OK'), a:has-text('OK')").first
                    if ok_btn.is_visible(timeout=2000):
                        ok_btn.click()
                except:
                    pass
            else:
                # 🎉 SLOTS AVAILABLE — just flag it, don't touch the form.
                log.info("🎉🎉🎉 SLOTS DETECTED! 🎉🎉🎉")
                page.screenshot(path=str(LOG_DIR / "slots_available.png"))

                if should_notify():
                    send_email_notification(
                        "PRENOTAMI: Schengen Visa Slot Available!",
                        f"A Schengen visa slot appears to be available at the Italian Consulate SF!\\n\\n"
                        f"Go book it now: https://prenotami.esteri.it/\\n\\n"
                        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\\n\\n"
                        f"-- PrenotaMi Checker"
                    )
                    mark_notified()
                else:
                    log.info("Within notification cooldown — skipping duplicate email.")

        except Exception as e:
            log.error(f"Error: {e}")
            try:
                page.screenshot(path=str(LOG_DIR / "error.png"))
            except:
                pass
        finally:
            browser.close()

    log.info("Check complete.")


def run_loop():
    """Run the checker in a loop."""
    log.info(f"Starting check loop (interval: {CHECK_INTERVAL}s = {CHECK_INTERVAL//60} min)...")
    while True:
        if is_already_booked():
            log.info("✅ Already booked! Exiting loop.")
            break
        check_for_slots()
        log.info(f"Next check in {CHECK_INTERVAL // 60} minutes...")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="PrenotaMi Schengen Visa Slot Checker")
    parser.add_argument("--loop", action="store_true", help="Run in continuous loop mode")
    parser.add_argument("--once", action="store_true", help="Run a single check (default)")
    args = parser.parse_args()

    if args.loop:
        run_loop()
    else:
        check_for_slots()
