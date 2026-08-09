#!/usr/bin/env python3
"""
YouTube Cookie Service — Render.com (Docker)
Uses system Chromium + Playwright with robust fallbacks.
"""

import os
import base64
import time
import threading
import logging
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── Config ──────────────────────────────────────────────────────────────
GOOGLE_EMAIL   = os.environ.get("GOOGLE_EMAIL", "")
GOOGLE_PASS    = os.environ.get("GOOGLE_PASS", "")
RENDER_SECRET  = os.environ.get("RENDER_SECRET", "")
COOKIE_MAX_AGE = int(os.environ.get("COOKIE_MAX_AGE", "72"))
CHROMIUM_PATH  = os.environ.get("CHROMIUM_PATH", "/usr/bin/chromium")
RETRY_COUNT    = int(os.environ.get("RETRY_COUNT", "3"))

# ── In-memory cache ────────────────────────────────────────────────────
_cache = {
    "cookie_text":   None,
    "extracted_at":  None,
    "extract_count": 0,
    "last_error":    None,
    "extracting":    False,
}
_cache_lock = threading.Lock()

# ── Auth ──────────────────────────────────────────────────────────────
def is_authorized():
    secret = request.headers.get("X-Render-Secret", "")
    return secret == RENDER_SECRET and RENDER_SECRET != ""

# ── Core extraction with multiple strategies ─────────────────────────
def extract_cookies():
    """Extract cookies – tries multiple approaches with retries."""
    last_exception = None
    for attempt in range(1, RETRY_COUNT + 1):
        logger.info(f"Extraction attempt {attempt}/{RETRY_COUNT}")
        try:
            return _extract_cookies_internal()
        except Exception as e:
            logger.warning(f"Attempt {attempt} failed: {e}")
            last_exception = e
            time.sleep(5 * attempt)  # backoff
    raise Exception(f"All {RETRY_COUNT} attempts failed. Last error: {last_exception}")

def _extract_cookies_internal():
    """Internal extraction – one attempt."""
    with sync_playwright() as p:
        logger.info("Launching Chromium...")
        browser = p.chromium.launch(
            executable_path=CHROMIUM_PATH,
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
                "--window-size=412,915",
            ]
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/116.0.0.0 Mobile Safari/537.36"
            ),
            viewport={"width": 412, "height": 915},
        )
        page = context.new_page()

        # Save debug artifacts on failure
        def save_debug_info():
            try:
                page.screenshot(path="/tmp/debug_latest.png")
                with open("/tmp/debug_latest.html", "w") as f:
                    f.write(page.content())
                logger.info("Saved debug screenshot and HTML to /tmp/")
            except:
                pass

        try:
            # ── Strategy: Start from YouTube, click "Sign in" ──
            logger.info("Navigating to YouTube...")
            page.goto("https://www.youtube.com", wait_until="networkidle", timeout=30000)
            # Wait for the sign-in button (could be text or a[href])
            sign_in_clicked = False
            for selector in ['text="Sign in"', 'a[href*="accounts.google.com"]', 'ytd-button-renderer:has-text("Sign in")']:
                try:
                    page.wait_for_selector(selector, state="visible", timeout=5000)
                    page.click(selector)
                    sign_in_clicked = True
                    logger.info(f"Clicked sign-in using selector: {selector}")
                    break
                except:
                    continue
            if not sign_in_clicked:
                # Fallback: go directly to accounts.google.com
                logger.info("Sign-in button not found; going directly to accounts.google.com")
                page.goto("https://accounts.google.com/", wait_until="networkidle", timeout=30000)

            # ── Wait for either email field or "Choose an account" ──
            # Check for "Choose an account" screen
            try:
                choose_account = page.wait_for_selector('div[data-challengetype]', timeout=3000)  # not reliable
                # better: check for text
                if page.locator('text="Choose an account"').is_visible():
                    logger.info("Detected 'Choose an account' screen.")
                    # Click "Use another account" or the "Add account" button
                    try:
                        page.click('text="Use another account"')
                    except:
                        page.click('text="Add account"')
                    # Wait for email field to appear
                    page.wait_for_selector('#identifierId', state="visible", timeout=10000)
            except:
                # No choose account; proceed with email field
                pass

            # ── Enter email ──
            logger.info("Entering email...")
            # Try multiple selectors for email input
            email_selectors = ['#identifierId', 'input[type="email"]', 'input[name="identifier"]']
            email_filled = False
            for sel in email_selectors:
                try:
                    page.wait_for_selector(sel, state="visible", timeout=5000)
                    page.fill(sel, GOOGLE_EMAIL)
                    email_filled = True
                    logger.info(f"Filled email using selector: {sel}")
                    break
                except:
                    continue
            if not email_filled:
                save_debug_info()
                raise Exception("Could not find email input field.")

            page.click('#identifierNext')
            # Wait for password field
            page.wait_for_selector('input[type="password"]', state="visible", timeout=15000)

            # ── Enter password ──
            logger.info("Entering password...")
            page.fill('input[type="password"]', GOOGLE_PASS)
            page.click('#passwordNext')

            # ── Wait for successful login ──
            logger.info("Waiting for login to complete...")
            try:
                page.wait_for_url(lambda url: "youtube.com" in url or "myaccount.google.com" in url, timeout=15000)
            except PlaywrightTimeout:
                # Maybe a CAPTCHA or 2FA page appears
                save_debug_info()
                raise Exception("Login did not navigate to expected URL – possible CAPTCHA or 2FA.")

            # ── Ensure we're on YouTube ──
            if "youtube.com" not in page.url:
                logger.info("Navigating to YouTube...")
                page.goto("https://www.youtube.com", wait_until="networkidle", timeout=30000)
            page.wait_for_selector('ytd-app', state="visible", timeout=10000)

            # ── Grab cookies ──
            logger.info("Grabbing cookies...")
            cookies = context.cookies([
                "https://www.youtube.com",
                "https://google.com",
                "https://accounts.google.com",
            ])
            if not cookies:
                raise Exception("No cookies found after login.")

            # Convert to Netscape format
            lines = ["# Netscape HTTP Cookie File\n"]
            for c in cookies:
                domain  = c["domain"]
                flag    = "TRUE" if domain.startswith(".") else "FALSE"
                path    = c.get("path", "/")
                secure  = "TRUE" if c.get("secure") else "FALSE"
                expires = int(c.get("expires") or 0)
                lines.append(
                    f"{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{c['name']}\t{c['value']}\n"
                )
            cookie_text = "".join(lines)
            logger.info(f"✅ Extracted {len(cookies)} cookies")
            return cookie_text

        except Exception as e:
            save_debug_info()
            raise e
        finally:
            browser.close()

# ── Cache wrapper (thread-safe) ──────────────────────────────────────
def get_cookies():
    """Return (cookie_text, is_fresh)."""
    with _cache_lock:
        if _cache["cookie_text"] and _cache["extracted_at"]:
            age = datetime.now() - _cache["extracted_at"]
            if age < timedelta(hours=COOKIE_MAX_AGE):
                logger.info(f"Using cached cookies (age: {age.total_seconds()/3600:.1f}h)")
                return _cache["cookie_text"], False

        if _cache["extracting"]:
            raise Exception("Extraction already in progress; please retry later.")

        logger.info("Cache expired or missing – starting fresh extraction...")
        _cache["extracting"] = True

    try:
        new_text = extract_cookies()
        with _cache_lock:
            _cache["cookie_text"]   = new_text
            _cache["extracted_at"]  = datetime.now()
            _cache["extract_count"] += 1
            _cache["last_error"]    = None
            _cache["extracting"]    = False
        return new_text, True
    except Exception as e:
        with _cache_lock:
            _cache["last_error"] = str(e)
            _cache["extracting"] = False
        logger.error(f"Extraction failed: {e}")
        raise

# ── Flask Routes ──────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    with _cache_lock:
        age_hrs = None
        if _cache["extracted_at"]:
            age_hrs = round(
                (datetime.now() - _cache["extracted_at"]).total_seconds() / 3600, 1
            )
        return jsonify({
            "service":        "YouTube Cookie Service",
            "status":         "extracting" if _cache["extracting"] else "ready",
            "has_cookies":    _cache["cookie_text"] is not None,
            "cookie_age_hrs": age_hrs,
            "max_age_hrs":    COOKIE_MAX_AGE,
            "extract_count":  _cache["extract_count"],
            "last_error":     _cache["last_error"],
        }), 200

@app.route("/cookies", methods=["GET"])
def serve_cookies():
    if not is_authorized():
        return jsonify({"error": "Unauthorized"}), 401

    try:
        text, is_fresh = get_cookies()
        b64 = base64.b64encode(text.encode("utf-8")).decode("utf-8")
        with _cache_lock:
            extracted_at_str = (
                _cache["extracted_at"].strftime("%Y-%m-%d %H:%M UTC")
                if _cache["extracted_at"] else None
            )
        return jsonify({
            "success":      True,
            "cookies_b64":  b64,
            "is_fresh":     is_fresh,
            "cookie_count": text.count("\n") - 1,
            "extracted_at": extracted_at_str,
        }), 200
    except Exception as e:
        if "already in progress" in str(e):
            return jsonify({"error": "Extraction in progress, please retry"}), 503
        return jsonify({"error": str(e)}), 500

@app.route("/force-refresh", methods=["POST"])
def force_refresh():
    if not is_authorized():
        return jsonify({"error": "Unauthorized"}), 401

    with _cache_lock:
        _cache["extracted_at"] = None  # invalidate

    def _do_refresh():
        try:
            logger.info("Force refresh started in background")
            get_cookies()
            logger.info("Force refresh completed")
        except Exception as e:
            logger.error(f"Force refresh failed: {e}")

    threading.Thread(target=_do_refresh, daemon=True).start()
    return jsonify({"message": "Refresh started – check / for status"}), 200

# ── Main ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
