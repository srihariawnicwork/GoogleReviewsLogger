# Viewing the dashboard from anywhere (securely)

Goal: open the dashboard on any device, anywhere — without leaking review data.

## Recommended: one domain, Cloudflare Tunnel + Access (login-gated, same-origin)

This is the clean option — the dashboard and the n8n endpoint sit on the **same
domain**, so there's no CORS, no mixed-content, and a real login in front. The
token in the page source problem goes away because Access requires SSO/email login.

### 1. Serve the dashboard on the VM
```bash
sudo apt-get install -y nginx
sudo cp dashboard/index.html /var/www/html/index.html
# nginx serves it on port 80
```
Set `DATA_URL` in `index.html` to a **relative, same-origin** path:
```js
const DATA_URL = "/webhook/dashboard-data";
```

### 2. Cloudflare Tunnel (free, gives HTTPS + a stable domain)
```bash
# install
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o cloudflared
sudo install cloudflared /usr/local/bin/
cloudflared tunnel login                     # opens browser, pick your CF domain
cloudflared tunnel create awnic-reviews
```
Config `~/.cloudflared/config.yml` — route `/webhook/*` to n8n, everything else to the dashboard (nginx), all on one hostname:
```yaml
tunnel: awnic-reviews
credentials-file: /home/ubuntu/.cloudflared/<TUNNEL_ID>.json
ingress:
  - hostname: reviews.yourdomain.com
    path: /webhook/*
    service: http://localhost:5678
  - hostname: reviews.yourdomain.com
    service: http://localhost:80
  - service: http_status:404
```
```bash
cloudflared tunnel route dns awnic-reviews reviews.yourdomain.com
sudo cloudflared service install    # run as a service (survives reboot)
```
Point n8n at the public URL so webhooks register correctly (in the n8n container/env):
```
N8N_HOST=reviews.yourdomain.com
WEBHOOK_URL=https://reviews.yourdomain.com/
N8N_PROTOCOL=https
```

### 3. Cloudflare Access (the login gate)
Cloudflare dashboard → Zero Trust → Access → Applications → Add self-hosted app:
- Domain: `reviews.yourdomain.com`
- Policy: Allow → emails ending `@awnic.com` (or a named list).

Now anyone opening `https://reviews.yourdomain.com` must log in; authenticated
requests to `/webhook/dashboard-data` carry the Access cookie automatically
(same origin), so the dashboard just works. Devices anywhere, gated by login.

---

## Quick alternative: Vercel page + tunnel + token (less private)

Faster to stand up, but **weaker**: the token lives in the page's JS, so anyone
who opens the Vercel page's source can read it. Fine for low-sensitivity or
short-term; prefer Access above for anything real.

1. n8n public over HTTPS: `cloudflared tunnel --url http://localhost:5678`
   → gives a `https://xxxx.trycloudflare.com` URL (ephemeral; use a named tunnel for stability).
2. Set the token in the workflow: in `Check Token`, replace `REPLACE_WITH_DASHBOARD_TOKEN`
   with a strong secret: `openssl rand -hex 24`.
3. In `index.html`:
   ```js
   const DATA_URL = "https://xxxx.trycloudflare.com/webhook/dashboard-data?token=YOUR_TOKEN";
   ```
4. Deploy the HTML only (not the workflow JSON) to Vercel:
   ```bash
   mkdir site && cp index.html site/ && cd site && npx vercel
   ```

---

## Either way
- **Set a strong token** in the `Check Token` node (`openssl rand -hex 24`) — it's your
  backstop even behind Access.
- The dashboard polls every 2 min, so all devices stay current within ~2 minutes.
- Many simultaneous viewers → each poll re-reads Excel; if you scale to lots of
  screens, switch to a periodically-generated static JSON (see DEPLOY.md notes).
