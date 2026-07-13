# Deploying: Ubuntu VM (pipeline) + Vercel (dashboard)

Two independent halves:
- **VM** runs the data pipeline — the scraper runner (cron) + n8n (Groq/Excel/Outlook).
- **Vercel** hosts the dashboard **and** a serverless function that reads Excel via
  Microsoft Graph. The dashboard no longer depends on n8n being publicly reachable.

VM project path used throughout: **`/home/user_sai/GoogleReviewsLogger/`**

```
cron ─▶ deploy/run_reviews.sh ─▶ apify_runner.py ─▶ Apify actor
                                     │ POST reviews
                                     ▼
                            n8n (on the VM) ─▶ Groq + Excel + Outlook
                                     │ writes rows to Excel Online
                                     ▼
   Vercel: /api/dashboard-data (reads Excel via Graph) ◀─ index.html polls every 2 min
```

---

## PART A — VM: runner + cron

### 1. Get the project onto the VM
```bash
# clone into the path used below
git clone <your-repo> /home/user_sai/GoogleReviewsLogger
# or: scp -r "Google Reviews/files" user_sai@VM_IP:/home/user_sai/GoogleReviewsLogger
cd /home/user_sai/GoogleReviewsLogger
```

### 2. Set up the runner
```bash
bash deploy/setup_vm.sh              # venv + requests + python-dotenv
nano .env                            # keys below
.venv/bin/python apify_runner.py     # test once
```
`.env`:
```
APIFY_TOKEN=apify_api_xxx
N8N_WEBHOOK_URL=http://localhost:5678/webhook/new-review
APIFY_REVIEWS_LIMIT=50
APP_TZ_OFFSET_HOURS=4
POST_DELAY_S=4
```

### 3. Cron — every 15 minutes
```bash
chmod +x /home/user_sai/GoogleReviewsLogger/deploy/run_reviews.sh
crontab -e
# add this line:
*/15 * * * * /home/user_sai/GoogleReviewsLogger/deploy/run_reviews.sh
```
Logs: `/home/user_sai/GoogleReviewsLogger/logs/reviews_YYYY-MM-DD.log`. `flock`
prevents overlapping runs. (15-min cadence runs the Apify actor ~96×/day — watch
Apify cost; Groq stays cheap via dedup.)

### 4. n8n on the VM (the review pipeline)
```bash
docker run -d --restart unless-stopped --name n8n -p 5678:5678 \
  -v /home/user_sai/.n8n:/home/node/.n8n \
  -e N8N_HOST=localhost -e WEBHOOK_URL=http://localhost:5678/ \
  docker.n8n.io/n8nio/n8n
```
In the n8n UI (`http://VM_IP:5678`):
- Import **`workflow_main.json`**, reconnect Groq + MS365 credentials, reselect
  workbook/worksheet/tables on the Excel/Outlook nodes, and **Activate** it.
- You do **not** need `dashboard/dashboard_data.workflow.json` anymore — the
  dashboard's data now comes from the Vercel function (Part B). (Keep it only if
  you also want an in-n8n data endpoint.)

n8n can stay **localhost-only** now — nothing external needs to reach it, because
the dashboard reads Excel directly via Graph, not via n8n.

---

## PART B — Vercel: dashboard + data API

Files live in **`vercel/`**: `index.html` (dashboard), `api/dashboard-data.js`
(serverless function), `package.json`, `vercel.json`, `.env.example`.

### 1. Azure AD app (lets the function read Excel, app-only)
1. Entra ID → App registrations → New registration → note **tenant id** + **client id**.
2. Certificates & secrets → New client secret → copy the **value**.
3. API permissions → Microsoft Graph → **Application** permissions →
   `Files.Read.All` (broad) *or* `Sites.Selected` (scoped to one SharePoint site,
   preferred) → **Grant admin consent**.
4. Find the workbook's IDs (needed as env vars):
   ```bash
   # with a Graph token, list the drive + item id of the .xlsx, e.g.:
   # GET /users/{user}/drive/root:/path/to/Google Reviews Logging.xlsx
   # -> response has "parentReference.driveId" (WORKBOOK_DRIVE_ID) and "id" (WORKBOOK_ITEM_ID)
   ```
   (Graph Explorer at developer.microsoft.com/graph/graph-explorer is the easy way.)

### 2. Deploy to Vercel
```bash
cd vercel
npx vercel            # first run links/creates the project
npx vercel --prod     # production deploy
```
Or connect the repo in the Vercel dashboard with **Root Directory = `vercel`**.

### 3. Set environment variables (Vercel → Settings → Environment Variables)
See `vercel/.env.example` for the full list:
`MS_TENANT_ID`, `MS_CLIENT_ID`, `MS_CLIENT_SECRET`, `WORKBOOK_DRIVE_ID`,
`WORKBOOK_ITEM_ID`, optional `OWN_TABLE` / `COMPETITOR_TABLE`, optional
`DASHBOARD_TOKEN`. Redeploy after setting them.

### 4. Privacy (do this — the data is internal)
The function keeps the Graph secret server-side, but its output is public unless
you gate viewing. Turn on **Vercel Password Protection / Vercel Authentication**,
or front the domain with **Cloudflare Access**. (The `DASHBOARD_TOKEN` is a weak
extra layer — the token would be visible in the page source — so use real auth.)

The dashboard (`vercel/index.html`) already calls `/api/dashboard-data`
(same-origin: no CORS, no mixed-content) and auto-refreshes every 2 minutes.

---

## Notes & gotchas
- **Complaint categories chart:** reads the `Reason for Review (AI Summarized)`
  column (which now holds the category). No separate `complaint_category` column needed.
- **App-only vs delegated:** n8n reads Excel as *you* (delegated); the Vercel
  function reads app-only. If the workbook lives in a personal OneDrive,
  `Files.Read.All` is org-wide — prefer moving it to SharePoint and using
  `Sites.Selected` scoped to that site.
- **Caching:** the function sends `Cache-Control: s-maxage=120`, so many viewers
  polling every 2 min hit Graph roughly once per window (avoids rate limits).
- **Timezone:** `timedatectl set-timezone Asia/Dubai` on the VM; `APP_TZ_OFFSET_HOURS`
  handles the actor's UTC timestamps.
- **Apify first run:** `APIFY_REVIEWS_LIMIT=50` backfills; `POST_DELAY_S` throttles Groq.
