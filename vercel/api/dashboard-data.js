// Vercel Serverless Function — returns AWNIC review data from Excel Online.
// Reads the workbook via Microsoft Graph (app-only / client credentials).
// The Graph secret stays server-side (Vercel env var); the browser never sees it.
//
// Vercel → Project → Settings → Environment Variables:
//   MS_TENANT_ID        Azure AD tenant (directory) id
//   MS_CLIENT_ID        app registration client id
//   MS_CLIENT_SECRET    app registration client secret
//   WORKBOOK_DRIVE_ID   drive id containing the workbook
//   WORKBOOK_ITEM_ID    driveItem id of the .xlsx
//   OWN_SHEET           worksheet (tab) name holding the own-review data (the =SORT sheet)
//   COMPETITOR_TABLE    (optional) Excel table name for competitor data (default CompetitorReviewsTable)
//   DASHBOARD_TOKEN     (optional) if set, requests must pass ?token=<it>
//
// Requires Node 18+ (global fetch), which Vercel provides by default.

const GRAPH = 'https://graph.microsoft.com/v1.0';

async function getToken() {
  const tenant = process.env.MS_TENANT_ID;
  const body = new URLSearchParams({
    client_id: process.env.MS_CLIENT_ID,
    client_secret: process.env.MS_CLIENT_SECRET,
    scope: 'https://graph.microsoft.com/.default',
    grant_type: 'client_credentials',
  });
  const r = await fetch(`https://login.microsoftonline.com/${tenant}/oauth2/v2.0/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  });
  if (!r.ok) throw new Error(`token ${r.status}: ${(await r.text()).slice(0, 200)}`);
  return (await r.json()).access_token;
}

// Read a WORKSHEET's used range as objects keyed by the header row.
// Works with dynamic-array (=SORT) sheets — no Excel Table required.
// Assumes row 1 is the header row and the sheet holds only this data.
async function readSheet(token, sheet) {
  const drive = process.env.WORKBOOK_DRIVE_ID;
  const item = process.env.WORKBOOK_ITEM_ID;
  const url = `${GRAPH}/drives/${drive}/items/${item}/workbook/worksheets/`
            + `${encodeURIComponent(sheet)}/usedRange(valuesOnly=true)?$select=values`;
  const r = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
  if (!r.ok) throw new Error(`sheet ${sheet} ${r.status}`);

  const values = (await r.json()).values || [];
  if (values.length < 2) return [];               // header only / empty
  const headers = values[0].map((h) => String(h == null ? '' : h).trim());
  // A =SORT spill pads unpopulated rows with 0s — treat 0/blank-only rows as empty.
  const meaningful = (c) => {
    if (c === '' || c === null || c === undefined) return false;
    if (c === 0 || c === '0') return false;
    if (typeof c === 'string' && !c.trim()) return false;
    return true;
  };
  return values.slice(1)
    .filter((row) => row.some(meaningful))
    .map((row) => {
      const obj = {};
      headers.forEach((h, i) => { if (h) obj[h] = row[i]; });
      return obj;
    });
}

// Read an Excel TABLE's rows as objects keyed by column header.
async function readTable(token, table) {
  const drive = process.env.WORKBOOK_DRIVE_ID;
  const item = process.env.WORKBOOK_ITEM_ID;
  const base = `${GRAPH}/drives/${drive}/items/${item}/workbook/tables/${encodeURIComponent(table)}`;
  const headers = { Authorization: `Bearer ${token}` };
  const [colsRes, rowsRes] = await Promise.all([
    fetch(`${base}/columns?$select=name`, { headers }),
    fetch(`${base}/rows?$select=values`, { headers }),
  ]);
  if (!colsRes.ok) throw new Error(`columns ${table} ${colsRes.status}`);
  if (!rowsRes.ok) throw new Error(`rows ${table} ${rowsRes.status}`);
  const cols = (await colsRes.json()).value.map((c) => c.name);
  const rows = (await rowsRes.json()).value;
  return rows.map((row) => {
    const cells = row.values[0] || [];
    const obj = {};
    cols.forEach((name, i) => { obj[name] = cells[i]; });
    return obj;
  });
}

module.exports = async function handler(req, res) {
  try {
    const required = process.env.DASHBOARD_TOKEN;
    if (required && req.query.token !== required) {
      return res.status(401).json({ error: 'unauthorized' });
    }

    const token = await getToken();
    const [own, competitor, placeStats] = await Promise.all([
      // Own reviews live on a =SORT worksheet (no table) -> read the used range.
      readSheet(token, process.env.OWN_SHEET || 'OwnReviews'),
      // Competitor reviews are a real Excel table -> read it as a table.
      readTable(token, process.env.COMPETITOR_TABLE || 'CompetitorReviewsTable').catch(() => []),
      // Official all-time Google rating + review count per place (own + competitors).
      readSheet(token, process.env.PLACESTATS_SHEET || 'PlaceStats').catch(() => []),
    ]);

    // Edge-cache so many viewers polling every ~2 min hit Graph at most once per window.
    res.setHeader('Cache-Control', 's-maxage=120, stale-while-revalidate=60');
    res.status(200).json({ generated_at: new Date().toISOString(), own, competitor, placeStats });
  } catch (e) {
    res.status(500).json({ error: String((e && e.message) || e) });
  }
}
