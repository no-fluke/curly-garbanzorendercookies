#!/usr/bin/env python3
"""
YouTube Cookie Service — Render.com (Docker)
Uses system Chromium instead of Playwright's bundled browser.
"""

import os
import base64
import time
import threading
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright

app = Flask(__name__)

# ── Config — set in Render → Environment ──────────────────────────────────────
GOOGLE_EMAIL   = os.environ.get("GOOGLE_EMAIL",  "")
GOOGLE_PASS    = os.environ.get("GOOGLE_PASS",   "")
RENDER_SECRET  = os.environ.get("RENDER_SECRET", "")
COOKIE_MAX_AGE = int(os.environ.get("COOKIE_MAX_AGE", "72"))

# System Chromium path (installed via apt in Dockerfile)
CHROMIUM_PATH  = "/usr/bin/chromium"

# ── In-memory cache ───────────────────────────────────────────────────────────
_cache = {
    "cookie_text":   None,
    "extracted_at":  None,
    "extract_count": 0,
    "last_error":    None,
    "extracting":    False,
}
_lock = threading.Lock()

# ── Auth ──────────────────────────────────────────────────────────────────────
def is_authorized():
    secret = request.headers.get("X-Render-Secret", "")
    return secret == RENDER_SECRET and RENDER_SECRET != ""

# ── Extract cookies via system Chromium ───────────────────────────────────────
def extract_cookies():
    print(f"[{datetime.now()}] Launching system Chromium...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROMIUM_PATH,
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
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
            print("[extract] Opening YouTube...")
            page.goto("https://www.youtube.com", wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)

            print("[extract] Clicking Sign In...")
            page.click('a[href*="accounts.google.com"]', timeout=10000)
            time.sleep(2)

            print("[extract] Entering email...")
            page.fill('input[type="email"]', GOOGLE_EMAIL)
            page.click('#identifierNext')
            time.sleep(3)

            print("[extract] Entering password...")
            page.fill('input[type="password"]', GOOGLE_PASS)
            page.click('#passwordNext')
            time.sleep(5)

            print("[extract] Confirming login...")
            page.goto("https://www.youtube.com", wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)

            print("[extract] Grabbing cookies...")
            cookies = context.cookies([
                "https://www.youtube.com",
                "https://google.com",
                "https://accounts.google.com",
            ])
            if not cookies:
                raise Exception("No cookies found — login likely failed")

            # Convert to Netscape format for yt-dlp
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
            print(f"[extract] ✅ Got {len(cookies)} cookies")
            return cookie_text

        except Exception as e:
            try: page.screenshot(path="/tmp/debug.png")
            except: pass
            raise e
        finally:
            browser.close()

# ── Cache wrapper ─────────────────────────────────────────────────────────────
def get_cookies():
    with _lock:
        if _cache["cookie_text"] and _cache["extracted_at"]:
            age = datetime.now() - _cache["extracted_at"]
            if age < timedelta(hours=COOKIE_MAX_AGE):
                print(f"[cache] Using cached cookies (age: {age.total_seconds()/3600:.1f}h)")
                return _cache["cookie_text"], False

        print("[cache] Extracting fresh cookies...")
        _cache["extracting"] = True
        try:
            text = extract_cookies()
            _cache["cookie_text"]   = text
            _cache["extracted_at"]  = datetime.now()
            _cache["extract_count"] += 1
            _cache["last_error"]    = None
            _cache["extracting"]    = False
            return text, True
        except Exception as e:
            _cache["last_error"]  = str(e)
            _cache["extracting"]  = False
            raise

# ═══════════════════════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════════════════════

@app.route("/", methods=["GET"])
def health():
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
    """Called by Heroku on every restart."""
    if not is_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        text, is_fresh = get_cookies()
        b64 = base64.b64encode(text.encode("utf-8")).decode("utf-8")
        return jsonify({
            "success":      True,
            "cookies_b64":  b64,
            "is_fresh":     is_fresh,
            "cookie_count": text.count("\n") - 1,
            "extracted_at": (
                _cache["extracted_at"].strftime("%Y-%m-%d %H:%M UTC")
                if _cache["extracted_at"] else None
            ),
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/force-refresh", methods=["POST"])
def force_refresh():
    """Force re-extract cookies ignoring cache."""
    if not is_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    _cache["extracted_at"] = None
    _cache["cookie_text"]  = None
    def _do():
        try: get_cookies()
        except Exception as e: print(f"[force-refresh] ❌ {e}")
    threading.Thread(target=_do, daemon=True).start()
    return jsonify({"message": "Refresh started — check / for status"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
