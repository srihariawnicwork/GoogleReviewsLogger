# AWNIC Reviews Dashboard (Vercel)

Static dashboard + a serverless function that reads Excel Online via Microsoft
Graph. No n8n exposure needed — the dashboard calls its own `/api` route.

```
index.html            dashboard (fetches /api/dashboard-data, refresh 2 min)
api/dashboard-data.js  serverless: Graph client-credentials -> read Excel tables -> JSON
vercel.json            function config
.env.example           the env vars to set in Vercel (names only)
```

## Deploy
```bash
cd vercel
npx vercel --prod
```
(or connect the repo in Vercel with Root Directory = `vercel`).

Then set env vars in Vercel → Settings → Environment Variables (see `.env.example`)
and redeploy. Full setup — Azure AD app, workbook IDs, auth — is in the repo's
top-level `DEPLOY.md` (Part B).

## Auth
The function keeps the Graph secret server-side, but the output is public unless
you enable **Vercel Password Protection / Authentication** or **Cloudflare Access**.
Do that before sharing the URL — the data is internal.
