# How to run the project

Pipeline:

```
apify_runner.py ──calls──▶ Apify actor (web_wanderer/google-reviews-scraper)
        │                         │ returns reviews (review_id, rating, content,
        │◀────────────────────────┘ reviewer, dates, owner_response + its date)
        │  dedup / amendment / owner-reply state machine + field mapping
        ▼
   n8n webhook ─▶ Normalize ─▶ Is Own Review?
                                 ├ own  ─▶ Groq ─▶ Parse ─▶ Log/Update Excel ─▶ email
                                 └ comp ─▶ Shape ─▶ Log Competitor Excel
```

The **Apify store actor** does the scraping. **apify_runner.py** orchestrates,
keeps state, maps fields, and forwards to n8n. (The old `scraper.py` and the
custom `apify/` actor are superseded — keep `scraper.py` only as a local
fallback.)

## 1. One-time setup

**Apify**
- Create an account, subscribe/rent the actor `web_wanderer/google-reviews-scraper`.
- Copy your API token: console.apify.com → Settings → Integrations → API token.

**n8n** (local)
- Import `workflow_main.json`, reconnect credentials (Groq, MS365), reselect
  workbook/worksheet/tables on every Excel + Outlook node.
- Toggle the workflow **Active** (so the production webhook is registered).

**Excel** — `OwnReviewsTable` must contain these column headers (exact text):
```
review_id | Review Date | Department | Rating | Review | Customer Name in Google |
Reason for Review (AI Summarized) | Agent Name | Review Status | Branch |
Day | Month | Year | Suggested Reply | Response Status | Owner Reply |
Logged At | Customer Last Update | Agent Reply Time |
Latest Action Taken To Customer | Latest Action Date - Customer |
Latest Action Date - Internal | Complaint Resolution Days
```
`CompetitorReviewsTable`:
```
Date Scraped | Review Date | Competitor Name | Branch | Rating | Review |
Reviewer Name | Day | Month | Year
```
Both must be real Excel **Tables** (Insert → Table), named exactly as above.

## 2. Configure

`.env`:
```
APIFY_TOKEN=apify_api_xxxxxxxx
N8N_WEBHOOK_URL=http://localhost:5678/webhook/new-review
APIFY_REVIEWS_LIMIT=50
WEBHOOK_TIMEOUT_S=120
```
Edit `LOCATIONS` in `apify_runner.py` (one entry per place; `mode:"own"` or
`"competitor"`; put the competitor's name in `branch`).

## 3. Install + run
```powershell
pip install requests python-dotenv
python apify_runner.py
```

## 4. Schedule (pick one)
- **Windows Task Scheduler** → run `python apify_runner.py` every N hours
  (every 4–6h is plenty and keeps Apify cost low).
- **Apify-native:** schedule the actor in the Apify console and have it POST its
  dataset to an n8n webhook on finish (skips this runner, but then the
  dedup/amendment state logic must live in n8n).

## Notes
- The runner dedups across runs via `seen_reviews.json` — **don't delete it** in
  production, or already-logged reviews re-send.
- `Agent Reply Time` and `Customer Last Update` come straight from the actor
  (`owner_response_at_date`, `reviewed_at_date`) — real Google timestamps.
- For a cloud schedule, n8n must be reachable from the internet (the Apify
  cloud can't reach `localhost`). Running the runner locally avoids this.
