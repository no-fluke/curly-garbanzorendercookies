#!/usr/bin/env python3
"""
YouTube Cookie Service — Render.com (Docker)
Uses system Chromium + Playwright to extract YouTube cookies.
"""

import os
import base64
import time
import threading
import logging
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── Config from environment ──────────────────────────────────────────────
GOOGLE_EMAIL   = os.environ.get("GOOGLE_EMAIL", "")
GOOGLE_PASS    = os.environ.get("GOOGLE_PASS", "")
RENDER_SECRET  = os.environ.get("RENDER_SECRET", "")
COOKIE_MAX_AGE = int(os.environ.get("COOKIE_MAX_AGE", "72"))
CHROMIUM_PATH  = os.environ.get("CHROMIUM_PATH", "/usr/bin/chromium")
RETRY_COUNT    = int(os.environ.get("RETRY_COUNT", "3"))

# ── In-memory cache with lock ────────────────────────────────────────────
_cache = {
    "cookie_text":   None,
    "extracted_at":  None,
    "extract_count": 0,
    "last_error":    None,
    "extracting":    False,
}
_cache_lock = threading.Lock()

# ── Auth ──────────────────────────────────────────────────────────────────
def is_authorized():
    secret = request.headers.get("X-Render-Secret", "")
    return secret == RENDER_SECRET and RENDER_SECRET != ""

# ── Core extraction (with retries) ──────────────────────────────────────
def extract_cookies():
    """Extract cookies using system Chromium. Returns Netscape format string."""
    last_exception = None

    for attempt in range(1, RETRY_COUNT + 1):
        logger.info(f"Extraction attempt {attempt}/{RETRY_COUNT}")
        try:
            return _extract_cookies_internal()
        except Exception as e:
            logger.warning(f"Attempt {attempt} failed: {e}")
            last_exception = e
            # Wait longer between attempts
            time.sleep(5 * attempt)

    raise Exception(f"All {RETRY_COUNT} attempts failed. Last error: {last_exception}")

def _extract_cookies_internal():
    """Internal extraction logic – one attempt."""
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

        try:
            # 1. Go to Google Accounts (more predictable)
            logger.info("Navigating to accounts.google.com...")
            page.goto("https://accounts.google.com/", wait_until="networkidle", timeout=30000)

            # 2. Fill email
            logger.info("Entering email...")
            page.wait_for_selector('input[type="email"]', state="visible", timeout=10000)
            page.fill('input[type="email"]', GOOGLE_EMAIL)
            page.click('#identifierNext')
            # Wait for password field to appear
            page.wait_for_selector('input[type="password"]', state="visible", timeout=10000)

            # 3. Fill password
            logger.info("Entering password...")
            page.fill('input[type="password"]', GOOGLE_PASS)
            page.click('#passwordNext')

            # 4. Wait for login to complete – watch for redirect to YouTube or My Account
            # Either we land on a page with YouTube logo, or we check the URL.
            logger.info("Waiting for login to complete...")
            try:
                # Wait for navigation to YouTube or any Google service
                page.wait_for_url(lambda url: "youtube.com" in url or "myaccount.google.com" in url, timeout=15000)
            except PlaywrightTimeout:
                # Possibly we are stuck on a challenge page – take a screenshot for debugging
                page.screenshot(path="/tmp/login_fail.png")
                raise Exception("Login did not navigate to YouTube or My Account. Possibly 2FA/CAPTCHA.")

            # 5. Now navigate explicitly to YouTube to get the cookies
            logger.info("Navigating to YouTube...")
            page.goto("https://www.youtube.com", wait_until="networkidle", timeout=30000)
            # Wait for the YouTube home page to show some content
            page.wait_for_selector('ytd-app', state="visible", timeout=10000)

            # 6. Grab all cookies from the relevant domains
            logger.info("Grabbing cookies...")
            cookies = context.cookies([
                "https://www.youtube.com",
                "https://google.com",
                "https://accounts.google.com",
            ])

            if not cookies:
                raise Exception("No cookies found after login – likely login failed silently.")

            # Convert to Netscape format (for yt-dlp)
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
            # Save debug info
            try:
                page.screenshot(path="/tmp/debug_latest.png")
                with open("/tmp/debug_latest.html", "w") as f:
                    f.write(page.content())
            except:
                pass
            raise e
        finally:
            browser.close()

# ── Cache wrapper (thread-safe, keeps old cookie on failure) ──────────
def get_cookies():
    """Return (cookie_text, is_fresh) – if cache is stale or missing, try refresh."""
    with _cache_lock:
        # If we have a valid cached cookie and it's not too old, return it.
        if _cache["cookie_text"] and _cache["extracted_at"]:
            age = datetime.now() - _cache["extracted_at"]
            if age < timedelta(hours=COOKIE_MAX_AGE):
                logger.info(f"Using cached cookies (age: {age.total_seconds()/3600:.1f}h)")
                return _cache["cookie_text"], False

        # Otherwise, need to extract (or re-extract)
        if _cache["extracting"]:
            # Another thread is already extracting; we could wait or return stale?
            # For simplicity, wait a bit and then return whatever is in cache (maybe None)
            logger.warning("Extraction already in progress – waiting...")
            # We could implement a wait loop, but we'll raise a 503 to let client retry.
            # Let's raise an exception that the caller can convert to a 503.
            raise Exception("Extraction already in progress; please retry later.")

        logger.info("Cache expired or missing – starting fresh extraction...")
        _cache["extracting"] = True

    # Release the lock while we extract (so health checks can still read)
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
            # Keep the old cookie if any; we don't clear it.
        logger.error(f"Extraction failed: {e}")
        raise

# ── Flask Routes ─────────────────────────────────────────────────────────

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
    """Return base64-encoded Netscape cookies."""
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
        # If the error is "extraction in progress", return 503
        if "already in progress" in str(e):
            return jsonify({"error": "Extraction in progress, please retry"}), 503
        return jsonify({"error": str(e)}), 500

@app.route("/force-refresh", methods=["POST"])
def force_refresh():
    """Force re-extraction in background."""
    if not is_authorized():
        return jsonify({"error": "Unauthorized"}), 401

    with _cache_lock:
        # Invalidate cache but keep old cookie until new one is ready
        _cache["extracted_at"] = None  # this will trigger re-extraction on next GET

    # Start a background thread to perform extraction immediately
    def _do_refresh():
        try:
            logger.info("Force refresh started in background")
            get_cookies()
            logger.info("Force refresh completed")
        except Exception as e:
            logger.error(f"Force refresh failed: {e}")

    threading.Thread(target=_do_refresh, daemon=True).start()
    return jsonify({"message": "Refresh started – check / for status"}), 200

# ── Main ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
