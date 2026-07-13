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
 
APIFY_TOKEN = os.getenv("APIFY_TOKEN", "")
ACTOR_ID = os.getenv("APIFY_ACTOR_ID", "VpQAf550Awe9ey24X")  # kaix/google-maps-reviews-scraper
WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "https://your-n8n-instance/webhook/new-review")
WEBHOOK_TIMEOUT_S = int(os.getenv("WEBHOOK_TIMEOUT_S", "120"))
SEEN_PATH = Path(os.getenv("SEEN_REVIEWS_PATH", "./seen_reviews.json"))
REVIEWS_LIMIT = int(os.getenv("APIFY_REVIEWS_LIMIT", "5"))
 
# The actor returns timestamps in UTC. Convert to your local time for display
# (UAE / Gulf Standard Time = UTC+4). Set APP_TZ_OFFSET_HOURS to match yours.
TZ_OFFSET_HOURS = int(os.getenv("APP_TZ_OFFSET_HOURS", "4"))
LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET_HOURS))
 
# Pause between webhook POSTs so the downstream Groq call stays under the API's
# rate limit (free tier ~30 req/min). 4s => ~15 reviews/min, comfortably safe.
POST_DELAY_S = float(os.getenv("POST_DELAY_S", "4"))

# Tracks which places already had their PLACE STATS (official Google rating +
# total review count) forwarded today — sent once per day, not on all 96 runs.
STATS_STATE_PATH = Path(os.getenv("PLACE_STATS_STATE_PATH", "./place_stats_state.json"))

# OFF by default: place stats are seeded MANUALLY in the PlaceStats sheet and
# the dashboard increments them with each new scraped review. Only set this to
# true if the n8n "Is Place Stats?" branch exists — without it, these payloads
# would fall through to the competitor path and append junk rows.
SEND_PLACE_STATS = os.getenv("SEND_PLACE_STATS", "false").lower() == "true"
 
# One entry per place. Keep in sync with your monitoring list.
# mode "own" -> Groq analysis + notifications; anything else -> competitor log.
LOCATIONS = [
    {
        "url": "https://www.google.com/maps/place/Al+Wathba+Insurance+-+Head+Office,+Abu+Dhabi/@24.4884139,54.3706741,17z/data=!4m8!3m7!1s0x3e5e665d694d0647:0x7049fab4109a7727!8m2!3d24.488409!4d54.373249!9m1!1b1!16s%2Fg%2F1pv5w2h_8?entry=ttu&g_ep=EgoyMDI2MDYxNi4wIKXMDSoASAFQAw%3D%3D",
        "branch": "AWNIC-Abu Dhabi",
        "department": "Head Office - Abu Dhabi",
        "mode": "own",
    },
    {
        "url": "https://www.google.com/maps/place/Al+Wathba+Insurance+-+Abu+Dhabi+Traffic+Office/@24.4360855,54.4143106,17z/data=!4m8!3m7!1s0x3e5e6984cac2e373:0xc12744c85c9a02ea!8m2!3d24.4360806!4d54.4168855!9m1!1b1!16s%2Fg%2F11y490p20q?entry=ttu&g_ep=EgoyMDI2MDYxNi4wIKXMDSoASAFQAw%3D%3D",
        "branch": "AWNIC — Abu Dhabi",
        "department": "Traffic Office - Abu Dhabi",
        "mode": "own",
    },
    {
        "url": "https://www.google.com/maps/place/Al+Wathba+Insurance+-+Bateen+Traffic+Office,+Al+Ain/@24.2198263,55.6372141,17z/data=!4m8!3m7!1s0x3e8aad0a94c6d70b:0x9b17b1f2faaa4f3b!8m2!3d24.2198214!4d55.639789!9m1!1b1!16s%2Fg%2F11xsqd591n?entry=ttu&g_ep=EgoyMDI2MDYyNC4wIKXMDSoASAFQAw%3D%3D",
        "branch": "AWNIC-Al Ain",
        "department": "Bateen Traffic Office - Al Ain",
        "mode": "own"
    },
    {
        "url": "https://www.google.com/maps/place/Al+Wathba+Insurance+-+Musaffah+Traffic+Office/@24.3675037,54.5201815,17z/data=!4m8!3m7!1s0x3e5e47c1e3077041:0x26b3b960180fe15a!8m2!3d24.3674988!4d54.5227564!9m1!1b1!16s%2Fg%2F11xzt758d5?entry=ttu&g_ep=EgoyMDI2MDYyNC4wIKXMDSoASAFQAw%3D%3D",
        "branch": "AWNIC-Abu Dhabi",
        "department": "Musafah Traffic Office - Abu Dhabi",
        "mode": "own"
    },
    {
        "url": "https://www.google.com/maps/place/Al+Wathba+Insurance+-+Mahawi+Traffic+Office/@24.3213174,54.5863626,17z/data=!4m8!3m7!1s0x3e5e3949a56b5b0b:0x2374af0ca57d598a!8m2!3d24.3213125!4d54.5889375!9m1!1b1!16s%2Fg%2F11vwj9xtfw?entry=ttu&g_ep=EgoyMDI2MDYyNC4wIKXMDSoASAFQAw%3D%3D",
        "branch": "AWNIC-Abu Dhabi",
        "department": "Mahawi Traffic Office - Abu Dhabi",
        "mode": "own"
    },
    {
        "url": "https://www.google.com/maps/place/Al+Wathba+Insurance+-+Claims+Department,+Abu+Dhabi/@24.4199455,54.4624375,17z/data=!4m8!3m7!1s0x3e5e40cd5fb5559f:0xa6b91060744f3fe0!8m2!3d24.4199406!4d54.4650124!9m1!1b1!16s%2Fg%2F11bbwmq7qg?entry=ttu&g_ep=EgoyMDI2MDYyNC4wIKXMDSoASAFQAw%3D%3D",
        "branch": "AWNIC-Abu Dhabi",
        "department": "Claims Dept - Abu Dhabi",
        "mode": "own"
    },
    {
        "url": "https://www.google.com/maps/place/Al+Wathba+Insurance+-+Shamkha+Traffic+Office/@24.3990255,54.6899822,17z/data=!4m8!3m7!1s0x3e5e39b13d70b71d:0xeba27c881c4f86f8!8m2!3d24.3990206!4d54.6925571!9m1!1b1!16s%2Fg%2F11qg34xq9l?entry=ttu&g_ep=EgoyMDI2MDYyNC4wIKXMDSoASAFQAw%3D%3D",
        "branch": "AWNIC-Abu Dhabi",
        "department": "Shamkha Traffic Office - Abu Dhabi",
        "mode": "own"
    },
    {
        "url": "https://www.google.com/maps/place/Al+Wathba+Insurance+-+Dubai/@25.1856611,55.2731088,17z/data=!4m18!1m9!3m8!1s0x3e5f5cd1b7cc3c31:0x452d710e5b991e21!2sAl+Wathba+Insurance+-+Dubai!8m2!3d25.1856563!4d55.2756837!9m1!1b1!16s%2Fg%2F11bbwnlqjw!3m7!1s0x3e5f5cd1b7cc3c31:0x452d710e5b991e21!8m2!3d25.1856563!4d55.2756837!9m1!1b1!16s%2Fg%2F11bbwnlqjw?entry=ttu&g_ep=EgoyMDI2MDYyNC4wIKXMDSoASAFQAw%3D%3D",
        "branch": "AWNIC-Dubai",
        "department": "Claims Dept - Dubai",
        "mode": "own"
    },
    {
        "url": "https://www.google.com/maps/place/Al+Wathba+National+Insurance+-+Al+Ain+-+Claims+Department/@24.2218595,55.7552341,17z/data=!4m8!3m7!1s0x3e8ab1c2d01a939b:0xd8b1ab0e054a2aff!8m2!3d24.2218546!4d55.757809!9m1!1b1!16s%2Fg%2F11tjpll340?entry=ttu&g_ep=EgoyMDI2MDYyNC4wIKXMDSoASAFQAw%3D%3D",
        "branch": "AWNIC-Al Ain",
        "department": "Claims Dept - Al Ain",
        "mode": "own"
    },
    {
        "url": "https://www.google.com/maps/place/Al+Wathba+Insurance+-+Falaj+Hazza,+Al-Ain/@24.189341,55.7180031,17z/data=!4m8!3m7!1s0x3e8ab130cd2d2f43:0x69890699d034d6d1!8m2!3d24.1893361!4d55.720578!9m1!1b1!16s%2Fg%2F11n3m4xsgb?entry=ttu&g_ep=EgoyMDI2MDYyNC4wIKXMDSoASAFQAw%3D%3D",
        "branch": "AWNIC-Al Ain",
        "department": "Falaj Hazza - Al Ain",
        "mode": "own"
    },
    {
        "url": "https://www.google.com/maps/place/Al+Wathba+Insurance+-+Shamil+Nad+Al+Hamar,+Dubai/@25.201178,55.3738923,17z/data=!4m8!3m7!1s0x3e5f67f8eced5eb3:0xf97a6b454e56b7d5!8m2!3d25.2011732!4d55.3764672!9m1!1b1!16s%2Fg%2F11njn6t_jl?entry=ttu&g_ep=EgoyMDI2MDYyNC4wIKXMDSoASAFQAw%3D%3D",
        "branch": "AWNIC-Dubai",
        "department": "Shamil Nad Al Hamar - Dubai",
        "mode": "own"
    }
]
 
APIFY_RUN_SYNC = (
    f"https://api.apify.com/v2/acts/{ACTOR_ID}/run-sync-get-dataset-items"
)
 
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("apify_runner")
 
 
# ---------------------------------------------------------------------------
# STATE STORE (same schema/format as the local scraper)
# ---------------------------------------------------------------------------
def _normalize_state(v) -> dict:
    if isinstance(v, str):
        return {"fp": None, "status": v, "rating": None, "first_seen": None}
    s = dict(v) if isinstance(v, dict) else {}
    s.setdefault("fp", None)
    s.setdefault("status", "pending")
    s.setdefault("rating", None)
    s.setdefault("first_seen", None)
    return s
 
 
def load_seen() -> dict:
    if not SEEN_PATH.exists():
        return {}
    try:
        data = json.loads(SEEN_PATH.read_text())
        if isinstance(data, list):
            return {rid: _normalize_state("pending") for rid in data}
        return {rid: _normalize_state(v) for rid, v in data.items()}
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Could not read seen store, starting fresh: %s", e)
        return {}
 
 
def save_seen(seen: dict) -> None:
    SEEN_PATH.write_text(json.dumps(seen, indent=2, sort_keys=True))
 
 
def content_fingerprint(text: str, rating: int, last_edited: str = "") -> str:
    # Include lastEditedAt so an edit is detected even if the fetched text
    # looks identical (and to catch rating-only edits).
    return hashlib.sha256(f"{text}||{rating}||{last_edited}".encode()).hexdigest()[:16]
 
 
# ---------------------------------------------------------------------------
# DATE HELPERS — the actor returns ISO 8601 (e.g. 2025-08-20T03:48:03Z)
# ---------------------------------------------------------------------------
def _parse_iso(s: str):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    # UTC -> local display time
    if dt.tzinfo is not None:
        dt = dt.astimezone(LOCAL_TZ)
    return dt
 
 
def fmt_date(s: str) -> str:
    """Returns YYYY-MM-DD — unambiguous in any locale, so Excel never has
    to guess day-vs-month order and can never silently swap them."""
    dt = _parse_iso(s)
    return dt.strftime("%Y-%m-%d") if dt else ""
 
 
def fmt_datetime(s: str) -> str:
    """Returns full ISO 8601 datetime (YYYY-MM-DDTHH:MM:00) — same
    unambiguous-locale rationale as fmt_date(), with time precision kept
    so Excel stores it as a true date+time serial, not just a date."""
    dt = _parse_iso(s)
    return dt.strftime("%Y-%m-%dT%H:%M:00") if dt else ""
 
 
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
    log.info("Running actor for %s", location["branch"])
    # Retry transient network/DNS failures (e.g. getaddrinfo) with backoff.
    last_err = None
    for attempt in range(1, 4):
        try:
            resp = requests.post(
                APIFY_RUN_SYNC,
                params={"token": APIFY_TOKEN},
                json=payload,
                timeout=300,
            )
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
    log.error("  actor call gave up after 3 attempts: %s", last_err)
    return []
 
 
def build_payload(item: dict, location: dict) -> dict:
    # kaix/google-maps-reviews-scraper output (nested objects).
    author = item.get("author") or {}
    owner = item.get("ownerResponse") or {}
 
    rating = int(item.get("rating") or 0)
    review_text = item.get("text") or item.get("textTranslated") or ""
    reviewer = author.get("name") or "Anonymous"
    published = item.get("publishedAt") or ""
    last_edited = item.get("lastEditedAt") or ""
    owner_text = owner.get("text") or owner.get("textTranslated") or ""
    owner_published = owner.get("publishedAt") or ""
 
    review_date = fmt_date(published)
    return {
        "review_id": item.get("reviewId") or "",
        "Review Date": review_date,
        "Rating": rating,
        "Review": review_text,
        "Customer Name in Google": reviewer,
        "Reason for Review": "", "Agent Name": "", "Review Status": "",
        "Latest Action Taken To Customer": "", "Latest Action Date - Customer": "",
        "Latest Action Date - Internal": "", "Complaint Resolution Days": "",
        # "Branch" now holds the office (was the Department field). The old
        # company-level branch column has been dropped.
        "Branch": location["department"],
        "mode": location["mode"],
        # This branch's Google Maps reviews page — used as the email button link.
        "Review Link": location["url"],
        # Actual owner reply text + the REAL time the owner replied
        # (ownerResponse.publishedAt), converted to local TZ.
        "Owner Reply": owner_text,
        "Agent Reply Time": fmt_datetime(owner_published),
        "owner_responded": bool(owner_text),
        "action": "log_new",
        # review_date is now "YYYY-MM-DD" (ISO, unambiguous) — slice accordingly.
        # Old format was "DD/MM/YYYY" which required different slice indices;
        # if you ever revert fmt_date(), revert this too.
        "Year": review_date[:4], "Month": review_date[5:7], "Day": review_date[8:],
        "_published_at": published,
        "_last_edited": last_edited,
    }
 
 
# ---------------------------------------------------------------------------
# PLACE STATS — the place's OFFICIAL all-time Google rating + total review
# count ride along in the actor's output, so this costs no extra scraping.
# Forwarded once per day per place as mode="place_stats"; n8n upserts them
# into the PlaceStats sheet, which feeds the dashboard's all-time KPIs.
# ---------------------------------------------------------------------------
def extract_place_stats(items: list) -> dict | None:
    """Pull place-level rating/review-count from an actor item. Actors name
    these fields differently, so several candidate keys are tried; if none
    match, the item's keys are logged so the right key can be added."""
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


def maybe_send_place_stats(items: list, location: dict) -> None:
    """Send this place's official rating/count to n8n, at most once per day.
    No-op unless SEND_PLACE_STATS=true (manual-seed mode is the default)."""
    if not SEND_PLACE_STATS:
        return
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    try:
        state = json.loads(STATS_STATE_PATH.read_text()) if STATS_STATE_PATH.exists() else {}
    except (json.JSONDecodeError, OSError):
        state = {}
    key = location["department"]
    if state.get(key) == today:
        return
    stats = extract_place_stats(items)
    if not stats:
        return
    payload = {
        "mode": "place_stats",
        "Place": key,
        "Type": "own",
        "Google Rating": stats["rating"],
        "Total Reviews": stats["count"],
        "Updated At": datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:00"),
    }
    if post_to_webhook(payload):
        state[key] = today
        STATS_STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))
        log.info("  -> place stats sent for %s (%.1f★ · %s reviews all-time)",
                 key, stats["rating"], stats["count"])


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
    # Throttle so Groq (downstream of the webhook) stays under its rate limit.
    if POST_DELAY_S:
        time.sleep(POST_DELAY_S)
    return ok
 
 
# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def process_location(location: dict, seen: dict, now_str: str) -> int:
    items = run_actor_for_location(location)
    maybe_send_place_stats(items, location)
    sent = 0
    for item in items:
        payload = build_payload(item, location)
        rid = payload["review_id"]
        if not rid:
            continue
        rating = payload["Rating"]
        fp = content_fingerprint(payload["Review"], rating, payload["_last_edited"])
        owner_responded = payload["owner_responded"]
        # When the customer last touched the review: last edit if any, else published.
        reviewed_at = fmt_datetime(payload["_last_edited"] or payload["_published_at"]) or now_str
        prev = seen.get(rid)
 
        if prev is None:
            payload["action"] = "log_new"
            payload["Logged At"] = now_str
            payload["Customer Last Update"] = reviewed_at
            if post_to_webhook(payload):
                seen[rid] = {"fp": fp, "status": ("replied" if owner_responded else "pending"),
                             "rating": rating, "first_seen": now_str}
                sent += 1
                log.info("  -> new review %s (%s★, replied=%s) sent",
                         rid, rating, owner_responded)
            continue
 
        if prev.get("fp") is None:
            prev["fp"] = fp
            prev.setdefault("first_seen", now_str)
            prev.setdefault("rating", rating)
        payload["Logged At"] = prev.get("first_seen") or now_str
 
        if prev["fp"] != fp:
            payload["action"] = "amended"
            payload["previous_rating"] = prev.get("rating")
            payload["Customer Last Update"] = reviewed_at
            if post_to_webhook(payload):
                seen[rid] = {"fp": fp,
                             "status": ("replied" if owner_responded else "pending"),
                             "rating": rating, "first_seen": payload["Logged At"]}
                sent += 1
                log.info("  -> review %s amended (%s★ -> %s★)", rid,
                         prev.get("rating"), rating)
        elif prev["status"] == "pending" and owner_responded:
            payload["action"] = "mark_replied"
            # Prefer the actor's real owner_response_at_date; if it didn't
            # provide one, fall back to detection time (now) so the column
            # isn't blank — approximate to within the scrape interval.
            if not payload.get("Agent Reply Time"):
                payload["Agent Reply Time"] = now_str
            if post_to_webhook(payload):
                seen[rid] = {"fp": fp, "status": "replied",
                             "rating": prev.get("rating", rating),
                             "first_seen": payload["Logged At"]}
                sent += 1
                log.info("  -> owner reply detected for %s; flipped", rid)
        else:
            seen[rid] = prev
    return sent
 
 
def main() -> None:
    if not APIFY_TOKEN:
        raise SystemExit("APIFY_TOKEN is not set (.env).")
 
    seen = load_seen()
    log.info("Loaded %d known reviews", len(seen))
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
 
    total = 0
    try:
        for loc in LOCATIONS:
            try:
                total += process_location(loc, seen, now_str)
            except Exception as e:  # noqa: BLE001
                log.exception("Error processing %s: %s", loc["branch"], e)
    finally:
        save_seen(seen)
    log.info("Run complete — %d reviews forwarded", total)
 
 
if __name__ == "__main__":
    main()
 