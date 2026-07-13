"""
Google Maps Review Scraper — Polling Daemon
============================================

Scrapes public Google Maps review pages, deduplicates against a local JSON
store, and POSTs new reviews to an n8n webhook for downstream LLM analysis
and Excel logging.

Designed to be run as a cron job or systemd service (e.g. every 15 minutes).

Dependencies:
    pip install playwright playwright-stealth python-dotenv requests
    playwright install chromium
"""

import os
import re
import json
import time
import hashlib
import logging
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout
from playwright_stealth import Stealth

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
load_dotenv()

WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "https://your-n8n-instance/webhook/new-review")
SEEN_PATH = Path(os.getenv("SEEN_REVIEWS_PATH", "./seen_reviews.json"))
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"

# How long to wait for the n8n webhook to respond. With the webhook's Respond
# mode = "lastNode", n8n holds the connection until the whole chain (Groq ->
# Excel -> Outlook) finishes, which can take well over 15s on the first call.
# If you switch the webhook to "Respond immediately", you can drop this back.
WEBHOOK_TIMEOUT_S = int(os.getenv("WEBHOOK_TIMEOUT_S", "120"))

# Persistent browser profile. Google serves a stripped-down "limited view"
# (no reviews in the DOM at all) to anonymous, automation-flagged sessions.
# A persistent profile that has signed into Google once lifts that gate.
# Run `python scraper.py --login` once (headful) to create/authenticate it.
PROFILE_DIR = Path(os.getenv("PW_PROFILE_DIR", "./pw_profile")).resolve()

# One entry per physical location/branch you monitor.
# `url` should be the Google Maps "reviews" deep link for the place.
LOCATIONS = [
    {
        "url": "https://www.google.com/maps/place/Al+Wathba+Insurance+-+Head+Office,+Abu+Dhabi/@24.4884139,54.3706741,17z/data=!4m8!3m7!1s0x3e5e665d694d0647:0x7049fab4109a7727!8m2!3d24.488409!4d54.373249!9m1!1b1!16s%2Fg%2F1pv5w2h_8?entry=ttu&g_ep=EgoyMDI2MDYyMi4wIKXMDSoASAFQAw%3D%3D",
        "branch": "Abu Dhabi Main Office",
        "department": "Main",
        "mode": "own"
    },
    {
         "url": "https://www.google.com/maps/place/Abu+Dhabi+National+Insurance+Co.+(ADNIC)/@24.493339,53.2090315,9z/data=!4m12!1m2!2m1!1sADNIC+!3m8!1s0x3e5e6663e5c670d9:0x22a2058cd323ba56!8m2!3d24.493339!4d54.362596!9m1!1b1!15sCgVBRE5JQyIDiAEBkgERaW5zdXJhbmNlX2NvbXBhbnngAQA!16s%2Fg%2F1w0p42tj?entry=ttu&g_ep=EgoyMDI2MDYxNi4wIKXMDSoASAFQAw%3D%3D",
         "branch": "ADNIC — Abu Dhabi Branch",
         "department": "Main",
         "mode": "competitor"
    },

    # ── COMPETITOR EXAMPLE ──────────────────────────────────────────────
    # Any mode other than "own" routes to the competitor branch in n8n.
    # For competitors, put the COMPETITOR'S NAME in "branch" — the
    # Shape Competitor Row node logs it as "Competitor Name".
    # Uncomment and replace the URL with the competitor's Google Maps page:
    # {
    #     "url": "https://www.google.com/maps/place/COMPETITOR_HERE/...",
    #     "branch": "Competitor Co. — Dubai Branch",
    #     "department": "N/A",
    #     "mode": "competitor"
    # },
]

# Polling jitter — randomized to look less bot-like
MIN_DELAY_BETWEEN_LOCATIONS_S = 8
MAX_DELAY_BETWEEN_LOCATIONS_S = 22

# How many reviews to scroll-load per run. 20 is enough for a 15-min cadence
# on a normal-volume location; raise for first-run backfills.
MAX_REVIEWS_PER_RUN = 1

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("review_scraper")


# ---------------------------------------------------------------------------
# DEDUP STORE
# ---------------------------------------------------------------------------
def _normalize_state(rid_value) -> dict:
    """Coerce any legacy stored value into the current per-review state dict:
    {fp, status, rating, first_seen}. Older formats:
      - bare string "pending"/"replied"  (status only)
      - missing fp/rating/first_seen     (older dict)
    """
    if isinstance(rid_value, str):  # status-only legacy value
        return {"fp": None, "status": rid_value, "rating": None, "first_seen": None}
    state = dict(rid_value) if isinstance(rid_value, dict) else {}
    state.setdefault("fp", None)
    state.setdefault("status", "pending")
    state.setdefault("rating", None)
    state.setdefault("first_seen", None)
    return state


def load_seen() -> dict[str, dict]:
    """Return review_id -> state dict {fp, status, rating, first_seen}.

      fp         : content fingerprint hash(text+rating); detects amendments
      status     : "pending" (awaiting our reply) | "replied" (owner replied)
      rating     : last-seen star rating (for "was 5★ -> now 1★" alerts)
      first_seen : display timestamp of first log (stable "Logged At")

    Backward compatible with the old list-of-ids and id->status formats."""
    if not SEEN_PATH.exists():
        return {}
    try:
        with SEEN_PATH.open() as f:
            data = json.load(f)
        if isinstance(data, list):  # oldest format: list of ids
            return {rid: _normalize_state("pending") for rid in data}
        return {rid: _normalize_state(v) for rid, v in data.items()}
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Could not read seen store, starting fresh: %s", e)
        return {}


def save_seen(seen: dict[str, dict]) -> None:
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SEEN_PATH.open("w") as f:
        json.dump(seen, f, indent=2, sort_keys=True)


def review_id(reviewer_name: str, review_text: str, rating: int) -> str:
    """Fallback synthetic ID, used only when Google's own data-review-id is
    missing. Hash of (name + text + rating). NOTE: this changes if the review
    is edited, so it can't track amendments — that's why we prefer the stable
    data-review-id (see build_payload)."""
    payload = f"{reviewer_name}||{review_text}||{rating}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:20]


def content_fingerprint(review_text: str, rating: int) -> str:
    """Hash of the review's editable content. When a customer amends their
    review (rating and/or text), this changes while the review_id stays
    constant — that's how we detect an amendment."""
    payload = f"{review_text}||{rating}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


# ---------------------------------------------------------------------------
# DATE PARSING
# ---------------------------------------------------------------------------
# Google shows relative dates like "3 days ago", "a week ago", "2 months ago".
_REL_DATE_RX = re.compile(
    r"(a|an|\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago",
    re.IGNORECASE,
)

_UNIT_TO_DAYS = {
    "second": 0,
    "minute": 0,
    "hour": 0,
    "day": 1,
    "week": 7,
    "month": 30,
    "year": 365,
}


def parse_relative_date(text: str, now: Optional[datetime] = None) -> datetime:
    """Convert 'a week ago' / '3 months ago' into an approximate datetime."""
    now = now or datetime.now()
    m = _REL_DATE_RX.search(text or "")
    if not m:
        return now
    qty_raw, unit = m.group(1).lower(), m.group(2).lower()
    qty = 1 if qty_raw in ("a", "an") else int(qty_raw)
    days_back = qty * _UNIT_TO_DAYS.get(unit, 0)
    return now - timedelta(days=days_back)


# ---------------------------------------------------------------------------
# PAGE INTERACTION
# ---------------------------------------------------------------------------
def force_english_url(url: str) -> str:
    """Append/override `hl=en` (host language) on a Google Maps URL so the
    page renders English tab labels ("Reviews", "Sort", "Newest"), which our
    selectors depend on."""
    parts = urlparse(url)
    q = parse_qs(parts.query, keep_blank_values=True)
    q["hl"] = ["en"]
    new_query = urlencode(q, doseq=True)
    return urlunparse(parts._replace(query=new_query))


def dismiss_consent(page: Page) -> None:
    """Click through Google's cookie/consent banner if it appears."""
    selectors = [
        'button[aria-label*="Accept all" i]',
        'button[aria-label*="Reject all" i]',
        'form[action*="consent"] button',
        'button:has-text("Accept all")',
        'button:has-text("I agree")',
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1500):
                btn.click()
                page.wait_for_timeout(800)
                return
        except PWTimeout:
            continue
        except Exception:
            continue


def expand_all_more_buttons(page: Page) -> None:
    """Click every 'More' button so truncated reviews are fully loaded."""
    try:
        buttons = page.locator('button:has-text("More")').all()
        for b in buttons:
            try:
                if b.is_visible():
                    b.click(timeout=800)
                    page.wait_for_timeout(120)
            except Exception:
                pass
    except Exception:
        pass


def find_scroll_container(page: Page):
    """Return an ElementHandle for the *actual* scrollable reviews panel.

    Google rotates the panel's class names constantly, so matching by class
    is brittle. Instead we start from a rendered review card and walk up the
    DOM to the nearest ancestor that genuinely overflows vertically
    (overflow-y: auto/scroll AND scrollHeight > clientHeight). That ancestor
    is the reviews feed regardless of what its classes are called this week.

    Returns None if no scrollable container can be found.
    """
    handle = page.evaluate_handle(
        """
        () => {
          const card = document.querySelector('div[data-review-id], div.jftiEf');
          if (card) {
            let el = card.parentElement;
            while (el && el !== document.body) {
              const oy = getComputedStyle(el).overflowY;
              if ((oy === 'auto' || oy === 'scroll') &&
                  el.scrollHeight > el.clientHeight + 50) {
                return el;
              }
              el = el.parentElement;
            }
          }
          // Fallback: among known panel containers, return the first that
          // actually has scrollable overflow.
          const candidates = document.querySelectorAll(
            'div.m6QErb[aria-label], div.m6QErb.DxyBCb, div[role="feed"]'
          );
          for (const c of candidates) {
            if (c.scrollHeight > c.clientHeight + 50) return c;
          }
          return null;
        }
        """
    )
    # evaluate_handle wraps `null` in a JSHandle; as_element() yields None for it.
    return handle.as_element()


def scroll_review_feed(page: Page, max_items: int) -> None:
    """Scroll the reviews panel (NOT the map) until we have enough review
    cards or reach the bottom.

    Critical: we scroll the container's `scrollTop` directly via JS on the
    element handle. We never use page.mouse.wheel() — on a /maps/place/ URL
    the cursor sits over the map, so a wheel event zooms/pans the MAP instead
    of scrolling the reviews. That was the original bug.
    """
    container = find_scroll_container(page)
    if container is None:
        log.warning("Could not locate scrollable reviews container — skipping scroll")
        return

    last_count = 0
    stable_iterations = 0
    for _ in range(40):  # hard cap so we don't loop forever
        # Re-resolve the container periodically? Not needed — the handle stays
        # valid as long as the element isn't detached. Scroll to the bottom.
        try:
            container.evaluate("el => { el.scrollTop = el.scrollHeight; }")
        except Exception:
            # Element may have been re-rendered; try to re-acquire it once.
            container = find_scroll_container(page)
            if container is None:
                break
            container.evaluate("el => { el.scrollTop = el.scrollHeight; }")

        page.wait_for_timeout(random.randint(900, 1600))

        current_count = page.locator('div[data-review-id], div.jftiEf').count()
        if current_count >= max_items:
            break
        if current_count == last_count:
            stable_iterations += 1
            if stable_iterations >= 3:
                break  # hit the bottom
        else:
            stable_iterations = 0
        last_count = current_count


def extract_reviews(page: Page) -> list[dict]:
    """Pull structured review data from rendered DOM via JS evaluation.
    This is more robust than per-element locator chains because we touch
    the DOM once and handle missing fields in JS where it's cheap."""
    expand_all_more_buttons(page)

    raw = page.evaluate(
        """
        () => {
          // Each review card. Google uses these classes today; if they
          // change, update here. We also accept any element with
          // data-review-id as a fallback.
          const cards = Array.from(document.querySelectorAll(
            'div[data-review-id], div.jftiEf.fontBodyMedium'
          ));

          const seen = new Set();
          const out = [];
          for (const card of cards) {
            const id = card.getAttribute('data-review-id') || '';
            if (id && seen.has(id)) continue;
            if (id) seen.add(id);

            // Reviewer name
            const nameEl = card.querySelector('div.d4r55, .WNxzHc');
            // The name container can include a second line like "5 reviews"
            // (the reviewer's review count). Keep only the first line so the
            // synthesized review_id stays stable as that count changes.
            const name = nameEl
              ? nameEl.innerText.trim().split('\\n')[0].trim()
              : '';

            // Star rating — read aria-label, format "5 stars" or "Rated 5.0 out of 5"
            const starEl = card.querySelector('[role="img"][aria-label*="star" i], span.kvMYJc');
            let rating = null;
            if (starEl) {
              const lbl = starEl.getAttribute('aria-label') || '';
              const m = lbl.match(/(\\d+(?:\\.\\d+)?)/);
              if (m) rating = parseFloat(m[1]);
            }

            // Relative date label
            const dateEl = card.querySelector('span.rsqaWe, span.xRkPPb');
            const relDate = dateEl ? dateEl.innerText.trim() : '';

            // Review body. Once the "More" button is clicked, the full
            // text is in the same element.
            const textEl = card.querySelector('span.wiI7pd, div.MyEned span');
            const text = textEl ? textEl.innerText.trim() : '';

            // Owner/business response. Google renders it inside the card as a
            // div.CDe7pd block labelled "Response from the owner".
            const ownerBlock = card.querySelector('div.CDe7pd');
            const ownerResponded = !!ownerBlock ||
              Array.from(card.querySelectorAll('*')).some(
                el => /response from the owner/i.test((el.textContent || '').slice(0, 80)));

            // Capture the ACTUAL reply text the owner posted (may differ from
            // our AI-suggested reply). The reply text sits in a .wiI7pd inside
            // the owner block; fall back to the block text with the
            // "Response from the owner / <date>" header stripped off.
            let ownerResponseText = '';
            if (ownerBlock) {
              const replyEl = ownerBlock.querySelector('.wiI7pd, .wiI7pd span');
              ownerResponseText = (replyEl ? replyEl.innerText : ownerBlock.innerText)
                .replace(/^response from the owner\\s*/i, '')
                .replace(/^\\s*(a|an|\\d+)\\s+\\w+\\s+ago\\s*/i, '')
                .trim();
            }

            if (!name && !text) continue;
            out.push({ id, name, rating, relDate, text, ownerResponded, ownerResponseText });
          }
          return out;
        }
        """
    )
    return raw


# ---------------------------------------------------------------------------
# WEBHOOK
# ---------------------------------------------------------------------------
def build_payload(raw: dict, location: dict) -> dict:
    """Shape the scraped review into the exact JSON the n8n workflow expects.
    Field names mirror your Excel columns so the n8n -> Excel node mapping
    is one-to-one. LLM-derived fields are placeholders here."""
    review_text = raw.get("text") or ""
    reviewer = raw.get("name") or "Anonymous"
    rating = int(raw.get("rating") or 0)

    dt = parse_relative_date(raw.get("relDate") or "")
    review_date = dt.strftime("%d/%m/%Y")

    # Prefer Google's own stable review id (data-review-id) — it survives
    # edits, so it lets us track amendments. Fall back to the synthetic hash
    # only if Google didn't expose one on the card.
    rid = raw.get("id") or review_id(reviewer, review_text, rating)

    return {
        # Identity / dedup
        "review_id": rid,

        # Direct-to-Excel columns (scraper-sourced)
        "Review Date": review_date,
        "Rating": rating,
        "Review": review_text,
        "Customer Name in Google": reviewer,

        # LLM will fill these downstream — sent as empty strings so the
        # Excel row schema stays consistent end-to-end
        "Reason for Review": "",
        "Agent Name": "",
        "Review Status": "",

        # Action columns — n8n fills only the positive-reply path
        "Latest Action Taken To Customer": "",
        "Latest Action Date - Customer": "",

        # Per spec — always blank
        "Latest Action Date - Internal": "",
        "Complaint Resolution Days": "",

        # Location metadata — "Branch" now holds the office (was Department).
        "Branch": location["department"],

        # This branch's Google Maps reviews page — used as the email button link.
        "Review Link": location["url"],

        # Routing key — the n8n "Is Own Review?" switch checks $json.mode
        # ("own" => analysis+notify path, anything else => competitor path).
        "mode": location["mode"],

        # The actual reply the business posted on Google (may differ from the
        # AI-suggested reply). Empty until an owner reply appears.
        "Owner Reply": raw.get("ownerResponseText") or "",

        # Whether the business has already replied to this review on Google.
        "owner_responded": bool(raw.get("ownerResponded")),

        # What n8n should do with this payload. Set in the scrape loop:
        #   "log_new"      -> first time we've seen it: analyze + log + notify
        #   "amended"      -> content changed: re-analyze + update row + notify
        #   "mark_replied" -> previously pending, now has an owner reply:
        #                     flip Response Status to "Posted Reply"
        "action": "log_new",

        # Pivot-friendly date parts
        "Day": dt.strftime("%d"),
        "Month": dt.strftime("%m"),
        "Year": dt.strftime("%Y"),

        # Extras the LLM node needs but Excel doesn't
        "_relative_date_raw": raw.get("relDate") or "",
        "_scraped_at": datetime.now().isoformat(timespec="seconds"),
    }


def post_to_webhook(payload: dict) -> bool:
    try:
        resp = requests.post(WEBHOOK_URL, json=payload, timeout=WEBHOOK_TIMEOUT_S)
        if 200 <= resp.status_code < 300:
            return True
        log.error("Webhook %s returned %s: %s", WEBHOOK_URL, resp.status_code, resp.text[:200])
        return False
    except requests.RequestException as e:
        log.error("Webhook POST failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def scrape_location(page: Page, location: dict, seen: set[str]) -> int:
    url = force_english_url(location["url"])
    log.info("Scraping %s (%s)", location["branch"], url)
    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    dismiss_consent(page)

    # Wait for the page to fully settle before doing anything
    page.wait_for_timeout(3000)

    # Detect Google's anti-automation "limited view", which strips reviews out
    # of the DOM entirely. If we see it, the profile isn't authenticated —
    # scrolling/tab-clicking can't help, so bail early with a clear message.
    try:
        limited = page.get_by_text("limited view of Google Maps", exact=False)
        if limited.first.is_visible(timeout=1500):
            log.warning(
                "%s served a LIMITED VIEW — no reviews in DOM. "
                "Run `python scraper.py --login` to authenticate the profile.",
                location["branch"],
            )
            return 0
    except Exception:
        pass

    # NOTE: We deliberately do NOT pre-scroll here. The old code scrolled
    # div[role="main"] (a non-overflowing wrapper => no-op) and fell back to
    # mouse-wheel over the map (=> zoomed the map). The card-render wait +
    # scroll_review_feed() below scroll the correct overflowing container.

    card_sel = 'div[data-review-id], div.jftiEf'

    # A reviews deep-link often opens straight to the reviews panel, in which
    # case the cards are already present and we must NOT click the Reviews tab
    # (clicking it again can toggle back to Overview => "no reviews"). Only
    # click the tab if no cards have rendered yet.
    already_loaded = False
    try:
        page.wait_for_selector(card_sel, timeout=6000)
        already_loaded = True
    except PWTimeout:
        already_loaded = False

    if not already_loaded:
        # Click the Reviews tab to switch the panel onto the reviews list.
        try:
            reviews_tab = page.locator(
                'button[aria-label^="Reviews" i], '
                'button[aria-label*="reviews for" i], '
                'div[role="tab"]:has-text("Reviews"), '
                'button:has-text("Reviews")'
            ).first
            if reviews_tab.is_visible(timeout=3000):
                reviews_tab.click()
                page.wait_for_timeout(2000)
        except Exception:
            pass

        # Now wait for review cards to appear.
        try:
            page.wait_for_selector(card_sel, timeout=30_000)
        except PWTimeout:
            log.warning("No reviews rendered for %s — skipping", location["branch"])
            return 0

    # Sort by newest so the most recent reviews load first. The sort menu
    # items are <div role="menuitemradio"> (NOT <li>); options are exactly
    # "Most relevant" / "Newest" / "Highest rating" / "Lowest rating".
    try:
        sort_btn = page.locator('button[aria-label*="Sort" i]').first
        if sort_btn.is_visible(timeout=3000):
            sort_btn.click()
            page.wait_for_timeout(1000)
            newest_btn = page.get_by_role("menuitemradio", name="Newest", exact=True)
            if newest_btn.is_visible(timeout=2000):
                newest_btn.click()
                # Feed re-renders from the top in newest-first order.
                page.wait_for_timeout(2000)
                log.info("  sorted reviews by Newest")
            else:
                log.warning("  Sort menu opened but 'Newest' item not found")
        else:
            log.warning("  Sort button not found — using default order")
    except Exception as e:
        log.warning("  Could not set Newest sort (%s) — using default order", e)

    # Scroll the review feed to load more cards
    scroll_review_feed(page, MAX_REVIEWS_PER_RUN)

    raws = extract_reviews(page)
    log.info("  found %d total review cards", len(raws))

    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")

    new_count = 0
    amended_count = 0
    flipped_count = 0
    for raw in raws[:MAX_REVIEWS_PER_RUN]:
        payload = build_payload(raw, location)
        rid = payload["review_id"]
        rating = payload["Rating"]
        fp = content_fingerprint(payload["Review"], rating)
        owner_responded = payload["owner_responded"]
        prev = seen.get(rid)

        if prev is None:
            # First time we've seen this review — analyze + log + notify.
            payload["action"] = "log_new"
            payload["Logged At"] = now_str
            payload["Customer Last Update"] = now_str
            if post_to_webhook(payload):
                seen[rid] = {"fp": fp, "status": "pending",
                             "rating": rating, "first_seen": now_str}
                new_count += 1
                log.info("  -> new review from %s (%s★) sent",
                         payload["Customer Name in Google"], rating)
            else:
                log.warning("  -> failed to forward review %s; will retry next run", rid)
            continue

        # Known review. Establish a fingerprint baseline if it migrated in
        # from an older store format (so we don't fire a false "amended").
        if prev.get("fp") is None:
            prev["fp"] = fp
            prev.setdefault("first_seen", now_str)
            prev.setdefault("rating", rating)

        logged_at = prev.get("first_seen") or now_str
        payload["Logged At"] = logged_at

        if prev["fp"] != fp:
            # Content changed since last scrape — the customer amended it.
            payload["action"] = "amended"
            payload["previous_rating"] = prev.get("rating")
            payload["Customer Last Update"] = now_str
            if post_to_webhook(payload):
                # Re-analyzed downstream; reset to pending (needs a fresh reply).
                seen[rid] = {"fp": fp, "status": "pending",
                             "rating": rating, "first_seen": logged_at}
                amended_count += 1
                log.info("  -> review %s amended (%s★ -> %s★); re-sent",
                         rid, prev.get("rating"), rating)
            else:
                log.warning("  -> failed to send amendment for %s; will retry", rid)

        elif prev["status"] == "pending" and owner_responded:
            # Unchanged content, but an owner reply has appeared — flip status.
            payload["action"] = "mark_replied"
            payload["Agent Reply Time"] = now_str
            if post_to_webhook(payload):
                seen[rid] = {"fp": fp, "status": "replied",
                             "rating": prev.get("rating", rating), "first_seen": logged_at}
                flipped_count += 1
                log.info("  -> owner reply detected for %s; flipped to Replied", rid)
            else:
                log.warning("  -> failed to flip review %s; will retry next run", rid)

        else:
            # No change to send; persist any baseline updates made above.
            seen[rid] = prev

    if amended_count:
        log.info("  %d review(s) amended and re-sent", amended_count)
    if flipped_count:
        log.info("  %d review(s) flipped to Replied", flipped_count)
    return new_count + amended_count + flipped_count


def launch_context(p, headless: bool):
    """Open a PERSISTENT browser context backed by PROFILE_DIR.

    Unlike launch() + new_context() (a throwaway anonymous session that Google
    serves a reviews-stripped "limited view"), a persistent context keeps
    cookies and a signed-in Google session on disk across runs, so reviews
    render normally. There is no separate Browser object — the context owns
    the browser process and context.close() tears it all down.
    """
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            # Force the browser UI/content language to English. Google keys
            # Maps' language partly off this, not just the URL param.
            "--lang=en-US",
        ],
        viewport={"width": 1366, "height": 900},
        locale="en-US",
        timezone_id="America/New_York",
        # Sent on every request. Google uses Accept-Language to pick the
        # response language; without this it may fall back to the IP's
        # locale (Arabic for a UAE place) regardless of `locale` above.
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )


def login():
    """One-time interactive sign-in. Opens the persistent profile headful,
    lands on Google Maps, and waits for you to sign in. Once you're in, the
    session is saved into PROFILE_DIR and every later `python scraper.py` run
    reuses it — no more 'limited view'."""
    stealth = Stealth()
    with stealth.use_sync(sync_playwright()) as p:
        context = launch_context(p, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.google.com/maps?hl=en", wait_until="domcontentloaded")
        print("\n" + "=" * 70)
        print("  A browser window is open. Sign into your Google account")
        print("  (click the 'Sign in' button, top-right) and accept any")
        print("  cookie/consent prompts. Leave it on a normal Maps page.")
        print(f"  Your session will be saved to: {PROFILE_DIR}")
        print("=" * 70)
        input("\n  When you're signed in, press ENTER here to save & close... ")
        context.close()
        print("  Saved. You can now run `python scraper.py` normally.\n")


def main():
    seen = load_seen()
    log.info("Loaded %d previously-seen review IDs", len(seen))

    if not PROFILE_DIR.exists() or not any(PROFILE_DIR.iterdir()):
        log.warning(
            "Profile dir %s is empty — Google will likely show a 'limited "
            "view' with no reviews. Run `python scraper.py --login` first.",
            PROFILE_DIR,
        )

    # playwright-stealth wraps the Playwright instance and applies all stealth
    # patches automatically to every browser/context/page created from it.
    stealth = Stealth()

    with stealth.use_sync(sync_playwright()) as p:
        context = launch_context(p, headless=HEADLESS)
        page = context.pages[0] if context.pages else context.new_page()

        try:
            total_new = 0
            for i, loc in enumerate(LOCATIONS):
                try:
                    total_new += scrape_location(page, loc, seen)
                except Exception as e:
                    log.exception("Error scraping %s: %s", loc["branch"], e)

                if i < len(LOCATIONS) - 1:
                    delay = random.uniform(
                        MIN_DELAY_BETWEEN_LOCATIONS_S,
                        MAX_DELAY_BETWEEN_LOCATIONS_S,
                    )
                    log.info("  sleeping %.1fs before next location", delay)
                    time.sleep(delay)

            log.info("Run complete — %d new reviews forwarded", total_new)
        finally:
            save_seen(seen)
            context.close()


if __name__ == "__main__":
    import sys
    if "--login" in sys.argv:
        login()
    else:
        main()
