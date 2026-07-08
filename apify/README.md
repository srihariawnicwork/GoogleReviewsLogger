# Google Reviews → n8n (Apify Actor)

Cloud port of the local Playwright scraper. Same payload contract — the n8n
workflow needs no changes.

## What it does
For each location in the input it scrapes Google Maps reviews (forces English,
sorts Newest, scrolls the reviews panel, expands "More", reads `data-review-id`,
the owner-reply text, and owner-reply presence), then for each review decides:

- **log_new** — first time seen → POST for analysis + logging + notify
- **amended** — content fingerprint changed → POST to re-analyze + update row + notify
- **mark_replied** — owner reply now present → POST to flip status to "Posted Reply"

State (which reviews are pending/replied + content fingerprints) is kept in the
Actor's **Key-Value Store** under `SEEN_REVIEWS`, so it persists across runs.

## Deploy
```bash
npm i -g apify-cli
apify login
cd apify
apify push          # builds the Docker image and uploads the actor
```
Then in the Apify Console set the **Input** (locations, `webhookUrl`,
`maxReviewsPerRun`, `timezoneOffsetHours`) and add a **Schedule** (e.g. every
15 min).

## Avoiding Google's "limited view"
Anonymous cloud sessions are often served a reviews-stripped page. Two levers
in the input:
1. **proxyConfiguration** → Apify Residential proxy (default on).
2. **googleCookies** → cookies from a signed-in Google session (most reliable).

If the log shows `LIMITED VIEW`, add cookies and/or keep residential proxy on.

## Local run
```bash
cd apify
apify run -i '{"webhookUrl":"http://host.docker.internal:5678/webhook/new-review","locations":[...]}'
```
Note: a locally-running n8n is reachable from the Apify cloud only if its
webhook is exposed (e.g. tunnel / public URL). For cloud schedules, n8n must be
reachable from the internet.
