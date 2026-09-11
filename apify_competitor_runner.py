"""
Apify Competitor Runner — daily competitor review scrape
========================================================

Sibling of apify_runner.py, but for COMPETITOR locations only, so it can run
on its own (daily) cron while the own-review runner keeps its 15-minute cadence.

Same actor (kaix/google-maps-reviews-scraper), same n8n webhook, same payload
contract. Every payload carries mode="competitor", so n8n's "Is Own Review?"
switch routes it to Shape Competitor Row -> Log Competitor Review (append).

Because the competitor path in n8n is APPEND-ONLY (no upsert), this runner only
ever sends a review ONCE (log_new). No amendment or owner-reply tracking —
re-sending would just duplicate rows in the competitor sheet.

Run:  python apify_competitor_runner.py     (manually, or via daily cron)

Env (.env — shared with apify_runner.py):
    APIFY_TOKEN                  = your Apify API token
    N8N_WEBHOOK_URL              = http://localhost:5678/webhook/new-review
    SEEN_COMPETITOR_PATH         = ./seen_competitor_reviews.json  (optional)
    COMPETITOR_REVIEWS_LIMIT     = 20   (optional, max reviews/place/run)
    APP_TZ_OFFSET_HOURS          = 4    (optional, UTC->local for timestamps)
    COMP_POST_DELAY_S            = 1    (optional; competitor path skips Groq,
                                         so only a light throttle is needed)

Dependencies:  pip install requests python-dotenv
"""

import os
import json
import time
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
load_dotenv()

# Primary + backup Apify tokens. On a quota/auth failure (monthly limit hit,
# invalid/blocked key) the runner auto-rotates to the next token. Add backups
# via APIFY_TOKEN_2, APIFY_TOKEN_3, ... and/or comma-separated APIFY_TOKENS.
def _load_apify_tokens() -> list:
    toks = []
    if os.getenv("APIFY_TOKEN"):
        toks.append(os.getenv("APIFY_TOKEN"))
    toks += list((os.getenv("APIFY_TOKENS") or "").split(","))
    i = 2
    while os.getenv(f"APIFY_TOKEN_{i}"):
        toks.append(os.getenv(f"APIFY_TOKEN_{i}"))
        i += 1
    seen, out = set(), []
    for t in (x.strip() for x in toks):
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


APIFY_TOKENS = _load_apify_tokens()
APIFY_TOKEN = APIFY_TOKENS[0] if APIFY_TOKENS else ""
_apify_idx = 0                          # index of the token currently in use
ROTATE_STATUS = {401, 402, 403, 429}    # auth / quota / rate-limit -> try next key


def _current_apify_token() -> str:
    return APIFY_TOKENS[_apify_idx] if APIFY_TOKENS else ""


def _rotate_apify_token() -> bool:
    global _apify_idx
    if _apify_idx + 1 < len(APIFY_TOKENS):
        _apify_idx += 1
        log.warning("Apify token exhausted/blocked — switching to backup key #%d of %d",
                    _apify_idx + 1, len(APIFY_TOKENS))
        return True
    return False
ACTOR_ID = os.getenv("APIFY_ACTOR_ID", "VpQAf550Awe9ey24X")  # kaix/google-maps-reviews-scraper
WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "https://your-n8n-instance/webhook/new-review")
WEBHOOK_TIMEOUT_S = int(os.getenv("WEBHOOK_TIMEOUT_S", "120"))
SEEN_PATH = Path(os.getenv("SEEN_COMPETITOR_PATH", "./seen_competitor_reviews.json"))
REVIEWS_LIMIT = int(os.getenv("COMPETITOR_REVIEWS_LIMIT", "20"))

# The actor returns timestamps in UTC. Convert to local (UAE = UTC+4).
TZ_OFFSET_HOURS = int(os.getenv("APP_TZ_OFFSET_HOURS", "4"))
LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET_HOURS))

# Competitor path skips Groq, so only a light throttle between webhook POSTs
# (each POST still does a synchronous Excel append in n8n).
POST_DELAY_S = float(os.getenv("COMP_POST_DELAY_S", "1"))

# One entry per competitor place — same key format as apify_runner.py:
#   "branch"     = competitor company name (shown in the Competitor Name column)
#   "department" = office scraped (shown in the Branch column)
#   "mode"       = "competitor" (routes to the raw-log path in n8n)
LOCATIONS = [
    {
        "url": "https://www.google.com/maps/place/Orient+Insurance+PJSC,+Head+Office/@25.2104254,55.3723984,17z/data=!4m8!3m7!1s0x3e5f676f2a62455f:0x6e258c5d6ca20208!8m2!3d25.2104206!4d55.3749733!9m1!1b1!16s%2Fg%2F11cffj4_4?entry=ttu",
        "branch": "Orient Insurance",
        "department": "Head Office - Dubai",
        "mode": "competitor",
    },
    {
        "url": "https://www.google.com/maps/place/Abu+Dhabi+National+Insurance+Co.+(ADNIC)/@24.4933439,54.3600211,17z/data=!4m8!3m7!1s0x3e5e6663e5c670d9:0x22a2058cd323ba56!8m2!3d24.493339!4d54.362596!9m1!1b1!16s%2Fg%2F1w0p42tj?entry=ttu",
        "branch": "ADNIC",
        "department": "Head Office - Abu Dhabi",
        "mode": "competitor",
    },
    {
        "url": "https://www.google.com/maps/place/Sukoon+Insurance+(formerly+Oman+Insurance+Company)+-+Head+Office/@25.2682857,55.3138651,17z/data=!3m1!4b1!4m6!3m5!1s0x3e5f5d4ec7c7c831:0x67ade3e6443903d2!8m2!3d25.2682809!4d55.31644!16s%2Fg%2F11ppcq3m41?entry=ttu",
        "branch": "Sukoon Insurance",
        "department": "Head Office - Dubai",
        "mode": "competitor",
    },
    {
        "url": "https://www.google.com/maps/place/Daman+Insurance+-+HQ+Branch/@24.4182681,54.4245233,15z/data=!4m8!3m7!1s0x3e5e4272eff5e535:0xdb623274d97b1001!8m2!3d24.4182497!4d54.4429774!9m1!1b1!16s%2Fg%2F11bw4zfprp?entry=ttu",
        "branch": "Daman",
        "department": "HQ Branch - Abu Dhabi",
        "mode": "competitor",
    },
    {
        # Place is branded "GIG Gulf" on Google Maps (AXA Gulf's new name).
        "url": "https://www.google.com/maps/place/GIG+Gulf/@25.1809395,55.260387,17z/data=!3m1!5s0x3e5f6bb8f2787aff:0x26feb7da3b080a90!4m8!3m7!1s0x3e5f69ce41217de7:0xd8635c464c78cc49!8m2!3d25.1809347!4d55.2629619!9m1!1b1!16s%2Fg%2F1hm34f81k?entry=ttu",
        "branch": "AXA Gulf",
        "department": "Dubai",
        "mode": "competitor",
    },
    {
        "url": "https://www.google.com/maps/place/Liva+Insurance/@25.2297714,55.2842782,17z/data=!4m8!3m7!1s0x3e5f5d2b3801a2cb:0xbb4460f0f616ee00!8m2!3d25.2297666!4d55.2868531!9m1!1b1!16s%2Fg%2F12hk_h2r_?entry=ttu",
        "branch": "Liva",
        "department": "Dubai",
        "mode": "competitor",
    },
    {
        "url": "https://www.google.com/maps/place/Emirates+Insurance+Company+PSC/@25.2157085,55.2767551,17z/data=!4m8!3m7!1s0x3e5f428da6ee5bb5:0xbec190a3a57c6d5c!8m2!3d25.2157037!4d55.27933!9m1!1b1!16s%2Fg%2F11bbwn9q70?entry=ttu",
        "branch": "Emirates Insurance Company",
        "department": "Dubai",
        "mode": "competitor",
    },
    {
        "url": "https://www.google.com/maps/place/Dubai+Insurance+Head+Office/@25.2630534,55.3227297,17z/data=!3m1!5s0x3e5f5cc55f0ae823:0x387335456bf6567c!4m8!3m7!1s0x3e5f5cc55926e009:0x7a53387a736440d!8m2!3d25.2630486!4d55.3253046!9m1!1b1!16s%2Fg%2F1tf56bvn?entry=ttu",
        "branch": "Dubai Insurance Company",
        "department": "Head Office - Dubai",
        "mode": "competitor",
    },
    {
        "url": "https://www.google.com/maps/place/Salama+Insurance+%7C+Head+Office/@25.2326304,55.3084399,17z/data=!3m1!5s0x3e5f42cc7a867507:0xc879a4c8a55c600!4m8!3m7!1s0x3e5f42cc79f7f67f:0x408f7667fddd4f38!8m2!3d25.2326256!4d55.3110148!9m1!1b1!16s%2Fg%2F1hc3l1632?entry=ttu",
        "branch": "Salama",
        "department": "Head Office - Dubai",
        "mode": "competitor",
    },
    {
        "url": "https://www.google.com/maps/place/NGI+Head+Office/@25.2533985,55.3268213,17z/data=!4m8!3m7!1s0x3e5f5cd8941701b1:0xcfbbcffe8d04b22e!8m2!3d25.2533937!4d55.3293962!9m1!1b1!16s%2Fg%2F11c1v9dhg5?entry=ttu",
        "branch": "NGI",
        "department": "Head Office - Dubai",
        "mode": "competitor",
    },
    {
        "url": "https://www.google.com/maps/place/Al+Ain+Ahlia+Insurance+Company+-+Head+Office/@24.4508601,54.3872525,17z/data=!4m8!3m7!1s0x3e5e6889d2188d01:0x823223d214354e07!8m2!3d24.4508552!4d54.3898274!9m1!1b1!16s%2Fg%2F1q62mn432?entry=ttu",
        "branch": "Al Ain Ahlia Insurance Company",
        "department": "Head Office - Abu Dhabi",
        "mode": "competitor",
    },
    {
        "url": "https://www.google.com/maps/place/Watania+Takaful+%E2%80%93+Head+office,+Jebel+AIi,+Dubai/@24.9819561,55.0903406,17z/data=!4m8!3m7!1s0x3e5f69885f70457f:0x95a92875c23a2503!8m2!3d24.9819513!4d55.0929155!9m1!1b1!16s%2Fg%2F1xpwh7sm?entry=ttu",
        "branch": "Watania",
        "department": "Head Office - Jebel Ali, Dubai",
        "mode": "competitor",
    },
    {
        "url": "https://www.google.com/maps/place/Abu+Dhabi+National+Takaful+Co.+PSC/@25.1921497,55.2820845,17z/data=!4m8!3m7!1s0x3e5f5cddf0fd2467:0xa77577b7358fd6c4!8m2!3d25.1921449!4d55.2846594!9m1!1b1!16s%2Fg%2F11bbwmv92j?entry=ttu",
        "branch": "Abu Dhabi National Takaful",
        "department": "Dubai",
        "mode": "competitor",
    },
]

APIFY_RUN_SYNC = (
    f"https://api.apify.com/v2/acts/{ACTOR_ID}/run-sync-get-dataset-items"
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("apify_competitor_runner")


# ---------------------------------------------------------------------------
# STATE STORE — competitor path is append-only in n8n, so we only track
# which review_ids were already logged (send once, never again).
# ---------------------------------------------------------------------------
def load_seen() -> dict:
    if not SEEN_PATH.exists():
        return {}
    try:
        data = json.loads(SEEN_PATH.read_text())
        if isinstance(data, list):
            return {rid: {"first_seen": None} for rid in data}
        return dict(data)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Could not read seen store, starting fresh: %s", e)
        return {}


def save_seen(seen: dict) -> None:
    SEEN_PATH.write_text(json.dumps(seen, indent=2, sort_keys=True))


def review_id_fallback(name: str, text: str, rating: int) -> str:
    return hashlib.sha256(f"{name}||{text}||{rating}".encode()).hexdigest()[:20]


# ---------------------------------------------------------------------------
# DATE HELPERS — actor returns ISO 8601 UTC; convert to local, emit ISO dates
# (same conventions as apify_runner.py so Excel parses identically).
# ---------------------------------------------------------------------------
def _parse_iso(s: str):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(LOCAL_TZ)
    return dt


def fmt_date(s: str) -> str:
    dt = _parse_iso(s)
    return dt.strftime("%Y-%m-%d") if dt else ""


# ---------------------------------------------------------------------------
# APIFY + WEBHOOK
# ---------------------------------------------------------------------------
def run_actor_for_location(location: dict) -> list[dict]:
    """Run the actor synchronously for one place and return its review items."""
    payload = {
        "urls": [location["url"]],
        "sort": "newest",
        "language": "en",
        "maxReviews": REVIEWS_LIMIT,
    }
    log.info("Running actor for %s (%s)", location["branch"], location["department"])
    while True:  # rotate to a backup token on quota/auth failure
        last_err = None
        for attempt in range(1, 4):  # retry transient network/DNS failures
            try:
                resp = requests.post(
                    APIFY_RUN_SYNC,
                    params={"token": _current_apify_token()},
                    json=payload,
                    timeout=300,
                )
                if resp.status_code in ROTATE_STATUS:
                    log.error("Actor auth/quota error (%s): %s",
                              resp.status_code, resp.text[:200])
                    break  # stop retrying this key -> rotate below
                if resp.status_code >= 300:
                    log.error("Actor run failed (%s): %s", resp.status_code, resp.text[:300])
                    return []
                items = resp.json()
                log.info("  actor returned %d reviews", len(items))
                return items
            except requests.RequestException as e:
                last_err = e
                log.warning("  actor call attempt %d/3 failed (%s); retrying...",
                            attempt, type(e).__name__)
                time.sleep(5 * attempt)
        else:
            log.error("  actor call gave up after 3 attempts: %s", last_err)
            return []
        if not _rotate_apify_token():
            log.error("  all Apify tokens exhausted")
            return []


def build_payload(item: dict, location: dict) -> dict:
    """Shape a competitor review for n8n. mode="competitor" routes it to the
    Shape Competitor Row -> Log Competitor Review (append) branch; that node
    reads competitor_name, Branch, Review Date, Rating, Review,
    'Customer Name in Google' and Day/Month/Year."""
    author = item.get("author") or {}
    rating = int(item.get("rating") or 0)
    review_text = item.get("text") or item.get("textTranslated") or ""
    reviewer = author.get("name") or "Anonymous"
    published = item.get("publishedAt") or ""
    review_date = fmt_date(published)
    rid = item.get("reviewId") or review_id_fallback(reviewer, review_text, rating)
    return {
        "review_id": rid,
        "Review Date": review_date,
        "Rating": rating,
        "Review": review_text,
        "Customer Name in Google": reviewer,
        "competitor_name": location["branch"],
        "Branch": location["department"],
        "mode": location["mode"],
        "action": "log_new",
        "Year": review_date[:4], "Month": review_date[5:7], "Day": review_date[8:],
    }


# ---------------------------------------------------------------------------
# PLACE STATS — the competitor's OFFICIAL all-time Google rating + total
# review count (from the actor's place-level fields; no extra scraping).
# Sent every run (this runner is daily); n8n upserts into PlaceStats.
# ---------------------------------------------------------------------------
def extract_place_stats(items: list) -> dict | None:
    if not items:
        return None
    it = items[0]
    place = it.get("place") or it.get("placeInfo") or {}
    rating = (place.get("rating") or place.get("totalScore")
              or it.get("placeRating") or it.get("place_rating")
              or it.get("totalScore") or it.get("place_rating_avg"))
    count = (place.get("reviewsCount") or place.get("userRatingCount")
             or place.get("reviewCount") or it.get("placeReviewsCount")
             or it.get("place_reviews_count") or it.get("reviewsCount"))
    if rating is None or count is None:
        log.warning("  place stats not found in actor output; top-level keys: %s",
                    sorted(it.keys()))
        return None
    try:
        return {"rating": float(rating), "count": int(count)}
    except (TypeError, ValueError):
        return None


def send_place_stats(items: list, location: dict) -> None:
    # OFF by default — stats are seeded manually in the PlaceStats sheet and
    # incremented by the dashboard. Enable only if the n8n branch exists.
    if os.getenv("SEND_PLACE_STATS", "false").lower() != "true":
        return
    stats = extract_place_stats(items)
    if not stats:
        return
    payload = {
        "mode": "place_stats",
        "Place": location["branch"],          # competitor company name
        "Type": "competitor",
        "Google Rating": stats["rating"],
        "Total Reviews": stats["count"],
        "Updated At": datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:00"),
    }
    if post_to_webhook(payload):
        log.info("  -> place stats sent for %s (%.1f★ · %s reviews all-time)",
                 location["branch"], stats["rating"], stats["count"])


def post_to_webhook(payload: dict) -> bool:
    ok = False
    try:
        resp = requests.post(WEBHOOK_URL, json=payload, timeout=WEBHOOK_TIMEOUT_S)
        if 200 <= resp.status_code < 300:
            ok = True
        else:
            log.error("Webhook %s -> %s: %s", WEBHOOK_URL, resp.status_code, resp.text[:200])
    except requests.RequestException as e:
        log.error("Webhook POST failed: %s", e)
    if POST_DELAY_S:
        time.sleep(POST_DELAY_S)
    return ok


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def process_location(location: dict, seen: dict, now_str: str) -> int:
    items = run_actor_for_location(location)
    send_place_stats(items, location)
    sent = 0
    for item in items:
        payload = build_payload(item, location)
        rid = payload["review_id"]
        if not rid or rid in seen:
            continue  # already logged — competitor sheet is append-only
        if post_to_webhook(payload):
            seen[rid] = {"first_seen": now_str, "competitor": location["branch"]}
            sent += 1
            log.info("  -> new %s review (%s★) sent", location["branch"], payload["Rating"])
    return sent


def main() -> None:
    if not APIFY_TOKENS:
        raise SystemExit("No Apify token set (APIFY_TOKEN in .env).")
    log.info("Apify tokens loaded: %d (1 primary + %d backup)",
             len(APIFY_TOKENS), len(APIFY_TOKENS) - 1)

    seen = load_seen()
    log.info("Loaded %d known competitor reviews", len(seen))
    now_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%dT%H:%M:00")

    total = 0
    try:
        for loc in LOCATIONS:
            try:
                total += process_location(loc, seen, now_str)
            except Exception as e:  # noqa: BLE001
                log.exception("Error processing %s: %s", loc["branch"], e)
    finally:
        save_seen(seen)
    log.info("Run complete — %d competitor reviews forwarded", total)


if __name__ == "__main__":
    main()
