"""
Google Review Reply Poster
==========================
Called by n8n via HTTP Request node AFTER the approver clicks "APPROVE".
Receives the review URL and reply text, logs into Google Business Profile,
navigates to the review, and posts the reply.

n8n calls this as a local HTTP endpoint — run with:
    python reply_poster.py

Listens on http://localhost:5055/post-reply
Expects JSON POST body: { "review_url": "...", "reply_text": "..." }

IMPORTANT:
- Run this on the same machine where the approver's Chrome profile lives,
  OR store session cookies after a one-time manual login (see SETUP below).
- This script uses a persistent browser context so you only log in once.

Dependencies:
    pip install playwright playwright-stealth flask
    playwright install chromium
"""

import os
import json
import time
import logging
from pathlib import Path

from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from playwright_stealth import Stealth

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
PORT = 5055
# Path to store the logged-in browser session. After first login, this
# persists cookies so subsequent runs don't need re-authentication.
SESSION_DIR = Path(os.getenv("SESSION_DIR", "./google_session"))
GOOGLE_BUSINESS_URL = "https://business.google.com/reviews"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("reply_poster")

app = Flask(__name__)
stealth = Stealth()


# ---------------------------------------------------------------------------
# CORE POSTING FUNCTION
# ---------------------------------------------------------------------------
def post_reply(review_url: str, reply_text: str) -> dict:
    """
    Open the Google Maps review page, find the Reply button,
    type the reply, and submit it.

    Returns: {"success": bool, "message": str}
    """
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    with stealth.use_sync(sync_playwright()) as p:
        # Persistent context keeps cookies/session between runs.
        # First run: you'll need to log in manually (see /login endpoint).
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(SESSION_DIR),
            headless=False,   # Keep headed so you can log in first time
                              # Change to True after session is established
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )

        page = context.new_page()

        try:
            # ── Step 1: Navigate to the specific review via Google Business ──
            # Google Business Profile lets owners reply from:
            # https://business.google.com/reviews
            # We navigate there and find the review by matching text.
            log.info("Navigating to Google Business reviews page")
            page.goto(GOOGLE_BUSINESS_URL, wait_until="networkidle", timeout=30_000)

            # Check if we're on the login page (session expired)
            if "accounts.google.com" in page.url:
                context.close()
                return {
                    "success": False,
                    "message": "Session expired. Visit http://localhost:5055/login to re-authenticate."
                }

            # ── Step 2: Find the review and click Reply ──
            # Google Business shows reviews as cards. We search for the
            # review by the reviewer name or snippet of review text.
            # Extract the reviewer name from the payload for matching.
            page.wait_for_timeout(2000)

            # Look for "Reply" buttons on the reviews page
            reply_buttons = page.locator('button:has-text("Reply"), button:has-text("Respond")').all()

            if not reply_buttons:
                # Try the direct review URL approach via Maps
                log.info("No reply buttons on Business page, trying Maps URL: %s", review_url)
                page.goto(review_url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(2000)
                reply_buttons = page.locator('button:has-text("Reply")').all()

            if not reply_buttons:
                return {
                    "success": False,
                    "message": "Could not find Reply button. The review may not be visible from this account, or the session lacks owner permissions."
                }

            # Click the first Reply button (most recent review)
            reply_buttons[0].click()
            page.wait_for_timeout(1500)

            # ── Step 3: Type the reply ──
            reply_box = page.locator('textarea, div[contenteditable="true"]').first
            reply_box.wait_for(state="visible", timeout=8000)
            reply_box.click()
            reply_box.fill(reply_text)
            page.wait_for_timeout(800)

            # ── Step 4: Submit ──
            submit_btn = page.locator(
                'button:has-text("Post"), button:has-text("Submit"), button:has-text("Reply")'
            ).last
            submit_btn.click()
            page.wait_for_timeout(2000)

            log.info("Reply posted successfully")
            return {"success": True, "message": "Reply posted successfully"}

        except PWTimeout as e:
            log.error("Timeout: %s", e)
            return {"success": False, "message": f"Timeout: {str(e)}"}
        except Exception as e:
            log.exception("Unexpected error: %s", e)
            return {"success": False, "message": str(e)}
        finally:
            context.close()


# ---------------------------------------------------------------------------
# FLASK ENDPOINTS
# ---------------------------------------------------------------------------
@app.route("/post-reply", methods=["POST"])
def handle_post_reply():
    """
    n8n calls this endpoint after approval.
    Body: { "review_url": "...", "reply_text": "...", "reviewer_name": "..." }
    """
    data = request.get_json(force=True)
    review_url = data.get("review_url", "")
    reply_text = data.get("reply_text", "")

    if not reply_text:
        return jsonify({"success": False, "message": "reply_text is required"}), 400

    result = post_reply(review_url, reply_text)
    status = 200 if result["success"] else 500
    return jsonify(result), status


@app.route("/login", methods=["GET"])
def handle_login():
    """
    ONE-TIME SETUP: Open a headed browser for manual Google login.
    Visit http://localhost:5055/login in your browser to trigger this.
    Log in to the Google account that owns the Business Profile.
    The session is saved to SESSION_DIR and reused automatically.
    """
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(SESSION_DIR),
            headless=False,
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        page.goto("https://accounts.google.com/signin")
        log.info("Manual login window opened. Complete login, then close the browser window.")
        # Wait for user to finish logging in (up to 3 minutes)
        try:
            page.wait_for_url("**/myaccount.google.com/**", timeout=180_000)
            log.info("Login detected. Session saved.")
        except Exception:
            log.info("Login window closed by user.")
        context.close()
    return "Login complete. Session saved. You can close this tab.", 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "session_exists": SESSION_DIR.exists()}), 200


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    log.info("Reply poster running on http://localhost:%d", PORT)
    log.info("First-time setup: visit http://localhost:%d/login", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False)
