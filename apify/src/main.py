"""
Google Reviews Scraper — Apify Actor
====================================

Cloud port of the local Playwright scraper. Scrapes Google Maps reviews for
each configured location, detects new reviews / amendments / owner replies,
and POSTs them to the n8n webhook — the same payload contract the n8n
workflow already expects.

Differences from the local script (by design, for the Apify platform):
  • Config comes from the Actor INPUT (not hardcoded LOCATIONS / .env).
  • Dedup/amendment state lives in the Actor's Key-Value Store ("SEEN_REVIEWS"),
    so it persists across scheduled runs.
  • Optional Apify Proxy (residential) + optional Google session cookies to
    avoid the "limited view" Google serves anonymous/automation sessions.
"""

from __future__ import annotations

import re
import hashlib
import random
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import httpx
from apify import Actor
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ---------------------------------------------------------------------------
# DATE PARSING (same logic as the local scraper)
# ---------------------------------------------------------------------------
_REL_DATE_RX = re.compile(
    r"(a|an|\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", re.IGNORECASE
)
_UNIT_TO_DAYS = {"second": 0, "minute": 0, "hour": 0, "day": 1,
                 "week": 7, "month": 30, "year": 365}


def parse_relative_date(text: str, now: datetime) -> datetime:
    m = _REL_DATE_RX.search(text or "")
    if not m:
        return now
    qty_raw, unit = m.group(1).lower(), m.group(2).lower()
    qty = 1 if qty_raw in ("a", "an") else int(qty_raw)
    return now - timedelta(days=qty * _UNIT_TO_DAYS.get(unit, 0))


def force_english_url(url: str) -> str:
    parts = urlparse(url)
    q = parse_qs(parts.query, keep_blank_values=True)
    q["hl"] = ["en"]
    return urlunparse(parts._replace(query=urlencode(q, doseq=True)))


def review_id_fallback(name: str, text: str, rating: int) -> str:
    return hashlib.sha256(f"{name}||{text}||{rating}".encode()).hexdigest()[:20]


def content_fingerprint(text: str, rating: int) -> str:
    return hashlib.sha256(f"{text}||{rating}".encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# PAGE INTERACTION
# ---------------------------------------------------------------------------
async def dismiss_consent(page) -> None:
    for sel in ('button[aria-label*="Accept all" i]', 'button[aria-label*="Reject all" i]',
                'button:has-text("Accept all")', 'button:has-text("I agree")'):
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1500):
                await btn.click()
                await page.wait_for_timeout(800)
                return
        except Exception:
            continue


async def expand_more_buttons(page) -> None:
    try:
        for b in await page.locator('button:has-text("More")').all():
            try:
                if await b.is_visible():
                    await b.click(timeout=800)
                    await page.wait_for_timeout(120)
            except Exception:
                pass
    except Exception:
        pass


async def find_scroll_container(page):
    """Walk up from a review card to the nearest scrollable ancestor."""
    handle = await page.evaluate_handle(
        """
        () => {
          const card = document.querySelector('div[data-review-id], div.jftiEf');
          if (card) {
            let el = card.parentElement;
            while (el && el !== document.body) {
              const oy = getComputedStyle(el).overflowY;
              if ((oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight + 50) return el;
              el = el.parentElement;
            }
          }
          for (const c of document.querySelectorAll('div.m6QErb[aria-label], div.m6QErb.DxyBCb, div[role="feed"]')) {
            if (c.scrollHeight > c.clientHeight + 50) return c;
          }
          return null;
        }
        """
    )
    return handle.as_element()


async def scroll_review_feed(page, max_items: int) -> None:
    container = await find_scroll_container(page)
    if container is None:
        Actor.log.warning("Could not locate scrollable reviews container")
        return
    last, stable = 0, 0
    for _ in range(40):
        try:
            await container.evaluate("el => { el.scrollTop = el.scrollHeight; }")
        except Exception:
            container = await find_scroll_container(page)
            if container is None:
                break
            await container.evaluate("el => { el.scrollTop = el.scrollHeight; }")
        await page.wait_for_timeout(random.randint(900, 1600))
        count = await page.locator('div[data-review-id], div.jftiEf').count()
        if count >= max_items:
            break
        if count == last:
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
        last = count


# Extraction JS — identical fields to the local scraper (incl. data-review-id,
# owner-response presence AND text). Backslashes are doubled for the Python str.
_EXTRACT_JS = r"""
() => {
  const cards = Array.from(document.querySelectorAll('div[data-review-id], div.jftiEf.fontBodyMedium'));
  const seen = new Set();
  const out = [];
  for (const card of cards) {
    const id = card.getAttribute('data-review-id') || '';
    if (id && seen.has(id)) continue;
    if (id) seen.add(id);
    const nameEl = card.querySelector('div.d4r55, .WNxzHc');
    const name = nameEl ? nameEl.innerText.trim().split('\n')[0].trim() : '';
    const starEl = card.querySelector('[role="img"][aria-label*="star" i], span.kvMYJc');
    let rating = null;
    if (starEl) { const lbl = starEl.getAttribute('aria-label') || ''; const m = lbl.match(/(\d+(?:\.\d+)?)/); if (m) rating = parseFloat(m[1]); }
    const dateEl = card.querySelector('span.rsqaWe, span.xRkPPb');
    const relDate = dateEl ? dateEl.innerText.trim() : '';
    const textEl = card.querySelector('span.wiI7pd, div.MyEned span');
    const text = textEl ? textEl.innerText.trim() : '';
    const ownerBlock = card.querySelector('div.CDe7pd');
    const ownerResponded = !!ownerBlock || Array.from(card.querySelectorAll('*')).some(
      el => /response from the owner/i.test((el.textContent || '').slice(0, 80)));
    let ownerResponseText = '';
    if (ownerBlock) {
      const replyEl = ownerBlock.querySelector('.wiI7pd, .wiI7pd span');
      ownerResponseText = (replyEl ? replyEl.innerText : ownerBlock.innerText)
        .replace(/^response from the owner\s*/i, '')
        .replace(/^\s*(a|an|\d+)\s+\w+\s+ago\s*/i, '')
        .trim();
    }
    if (!name && !text) continue;
    out.push({ id, name, rating, relDate, text, ownerResponded, ownerResponseText });
  }
  return out;
}
"""


async def extract_reviews(page):
    await expand_more_buttons(page)
    return await page.evaluate(_EXTRACT_JS)


def build_payload(raw: dict, location: dict, now: datetime) -> dict:
    review_text = raw.get("text") or ""
    reviewer = raw.get("name") or "Anonymous"
    rating = int(raw.get("rating") or 0)
    dt = parse_relative_date(raw.get("relDate") or "", now)
    rid = raw.get("id") or review_id_fallback(reviewer, review_text, rating)
    return {
        "review_id": rid,
        "Review Date": dt.strftime("%d/%m/%Y"),
        "Department": location["department"],
        "Rating": rating,
        "Review": review_text,
        "Customer Name in Google": reviewer,
        "Reason for Review": "", "Agent Name": "", "Review Status": "",
        "Latest Action Taken To Customer": "", "Latest Action Date - Customer": "",
        "Latest Action Date - Internal": "", "Complaint Resolution Days": "",
        "Branch": location["branch"],
        "mode": location["mode"],
        "Owner Reply": raw.get("ownerResponseText") or "",
        "owner_responded": bool(raw.get("ownerResponded")),
        "action": "log_new",
        "Day": dt.strftime("%d"), "Month": dt.strftime("%m"), "Year": dt.strftime("%Y"),
        "_relative_date_raw": raw.get("relDate") or "",
        "_scraped_at": now.isoformat(timespec="seconds"),
    }


async def post_to_webhook(client: httpx.AsyncClient, url: str, payload: dict) -> bool:
    try:
        resp = await client.post(url, json=payload, timeout=120)
        if 200 <= resp.status_code < 300:
            return True
        Actor.log.error("Webhook %s -> %s: %s", url, resp.status_code, resp.text[:200])
        return False
    except Exception as e:  # noqa: BLE001
        Actor.log.error("Webhook POST failed: %s", e)
        return False


async def scrape_location(page, client, location, webhook_url, seen, max_items, now_str, now):
    url = force_english_url(location["url"])
    Actor.log.info("Scraping %s (%s)", location["branch"], url)
    await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    await dismiss_consent(page)
    await page.wait_for_timeout(3000)

    # Limited-view gate detection
    try:
        if await page.get_by_text("limited view of Google Maps", exact=False).first.is_visible(timeout=1500):
            Actor.log.warning("%s served a LIMITED VIEW — supply Google cookies or "
                              "residential proxy to see reviews.", location["branch"])
            return 0
    except Exception:
        pass

    card_sel = 'div[data-review-id], div.jftiEf'
    try:
        await page.wait_for_selector(card_sel, timeout=6000)
    except PWTimeout:
        for sel in ('button[aria-label^="Reviews" i]', 'div[role="tab"]:has-text("Reviews")',
                    'button:has-text("Reviews")'):
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=2500):
                    await loc.click()
                    await page.wait_for_timeout(2000)
                    break
            except Exception:
                pass
        try:
            await page.wait_for_selector(card_sel, timeout=30_000)
        except PWTimeout:
            Actor.log.warning("No reviews rendered for %s", location["branch"])
            return 0

    # Sort by Newest
    try:
        sort_btn = page.locator('button[aria-label*="Sort" i]').first
        if await sort_btn.is_visible(timeout=3000):
            await sort_btn.click()
            await page.wait_for_timeout(1000)
            newest = page.get_by_role("menuitemradio", name="Newest", exact=True)
            if await newest.is_visible(timeout=2000):
                await newest.click()
                await page.wait_for_timeout(2000)
                Actor.log.info("  sorted by Newest")
    except Exception:
        pass

    await scroll_review_feed(page, max_items)
    raws = await extract_reviews(page)
    Actor.log.info("  found %d review cards", len(raws))

    sent = 0
    for raw in raws[:max_items]:
        payload = build_payload(raw, location, now)
        rid = payload["review_id"]
        rating = payload["Rating"]
        fp = content_fingerprint(payload["Review"], rating)
        owner_responded = payload["owner_responded"]
        prev = seen.get(rid)

        if prev is None:
            payload["action"] = "log_new"
            payload["Logged At"] = now_str
            payload["Customer Last Update"] = now_str
            if await post_to_webhook(client, webhook_url, payload):
                seen[rid] = {"fp": fp, "status": "pending", "rating": rating, "first_seen": now_str}
                await Actor.push_data(payload)
                sent += 1
            continue

        if prev.get("fp") is None:
            prev["fp"] = fp
            prev.setdefault("first_seen", now_str)
            prev.setdefault("rating", rating)
        logged_at = prev.get("first_seen") or now_str
        payload["Logged At"] = logged_at

        if prev["fp"] != fp:
            payload["action"] = "amended"
            payload["previous_rating"] = prev.get("rating")
            payload["Customer Last Update"] = now_str
            if await post_to_webhook(client, webhook_url, payload):
                seen[rid] = {"fp": fp, "status": "pending", "rating": rating, "first_seen": logged_at}
                await Actor.push_data(payload)
                sent += 1
        elif prev["status"] == "pending" and owner_responded:
            payload["action"] = "mark_replied"
            payload["Agent Reply Time"] = now_str
            if await post_to_webhook(client, webhook_url, payload):
                seen[rid] = {"fp": fp, "status": "replied",
                             "rating": prev.get("rating", rating), "first_seen": logged_at}
                sent += 1
        else:
            seen[rid] = prev

    return sent


async def main() -> None:
    async with Actor:
        inp = await Actor.get_input() or {}
        locations = inp.get("locations") or []
        webhook_url = inp.get("webhookUrl")
        max_items = int(inp.get("maxReviewsPerRun", 20))
        tz_offset = int(inp.get("timezoneOffsetHours", 0))
        cookies = inp.get("googleCookies") or []

        if not webhook_url or not locations:
            raise ValueError("Input must include 'webhookUrl' and at least one 'locations' entry.")

        now = datetime.utcnow() + timedelta(hours=tz_offset)
        now_str = now.strftime("%d/%m/%Y %H:%M")

        # Persistent dedup/amendment state in the Key-Value Store
        store = await Actor.open_key_value_store()
        seen = await store.get_value("SEEN_REVIEWS") or {}
        Actor.log.info("Loaded %d known reviews from KV store", len(seen))

        # Optional Apify residential proxy (helps avoid the limited view)
        launch_kwargs = {"headless": True, "args": ["--lang=en-US", "--no-sandbox",
                                                    "--disable-blink-features=AutomationControlled"]}
        proxy_cfg = await Actor.create_proxy_configuration(actor_proxy_input=inp.get("proxyConfiguration"))
        if proxy_cfg:
            proxy_url = await proxy_cfg.new_url()
            pu = urlparse(proxy_url)
            launch_kwargs["proxy"] = {"server": f"{pu.scheme}://{pu.hostname}:{pu.port}",
                                      "username": pu.username, "password": pu.password}

        total = 0
        async with async_playwright() as p, httpx.AsyncClient() as client:
            browser = await p.chromium.launch(**launch_kwargs)
            context = await browser.new_context(
                viewport={"width": 1366, "height": 900}, locale="en-US",
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            )
            if cookies:
                await context.add_cookies(cookies)
                Actor.log.info("Injected %d Google session cookies", len(cookies))
            page = await context.new_page()

            try:
                for i, loc in enumerate(locations):
                    try:
                        total += await scrape_location(page, client, loc, webhook_url,
                                                       seen, max_items, now_str, now)
                    except Exception as e:  # noqa: BLE001
                        Actor.log.exception("Error scraping %s: %s", loc.get("branch"), e)
                    if i < len(locations) - 1:
                        await page.wait_for_timeout(random.randint(8000, 22000))
            finally:
                await store.set_value("SEEN_REVIEWS", seen)
                await context.close()
                await browser.close()

        Actor.log.info("Run complete — %d reviews forwarded", total)
