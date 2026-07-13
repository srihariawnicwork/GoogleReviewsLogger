# Google Reviews Scraper

A polling daemon that scrapes Google Maps review pages, deduplicates against
a local JSON store, and POSTs new reviews to an n8n webhook.

## Setup

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Install the headless Chromium browser
playwright install chromium

# 3. Configure your environment
cp .env.example .env
# Edit .env and set N8N_WEBHOOK_URL to the webhook URL from your n8n trigger node

# 4. Configure your locations
# Edit the LOCATIONS list near the top of scraper.py.
# Each entry needs: url (Google Maps reviews link), branch, department.
```

## Running

```bash
# One-off run
python scraper.py

# Recommended: cron every 15 minutes
*/15 * * * * cd /opt/review_scraper && /usr/bin/python3 scraper.py >> scraper.log 2>&1
```

## Webhook payload

Each new review is sent as a JSON POST to your n8n webhook. The payload
keys match your Excel column names one-to-one (LLM-derived fields arrive
empty and are filled by the n8n workflow):

```json
{
  "review_id": "a1b2c3d4e5f6g7h8i9j0",
  "Review Date": "15/05/2026",
  "Department": "Claims",
  "Rating": 5,
  "Review": "Sarah was incredibly helpful with my claim...",
  "Customer Name in Google": "John Smith",
  "Reason for Review": "",
  "Agent Name": "",
  "Review Status": "",
  "Latest Action Taken To Customer": "",
  "Latest Action Date - Customer": "",
  "Latest Action Date - Internal": "",
  "Complaint Resolution Days": "",
  "Branch": "Downtown",
  "Day": "15",
  "Month": "05",
  "Year": "2026",
  "_relative_date_raw": "3 days ago",
  "_scraped_at": "2026-05-18T14:32:11"
}
```

## Notes

- The relative date parser converts Google's "3 days ago" / "a week ago"
  into approximate calendar dates. For exact dates, you'd need to be
  signed in — and signing in defeats the public-scraper approach.
- `seen_reviews.json` grows by ~80 bytes per review. Even at 10k reviews
  it stays under 1 MB.
- If Google updates the review-card DOM, update the selectors in
  `extract_reviews()`. The current selectors target the stable
  `data-review-id` attribute first and fall back to class names.
