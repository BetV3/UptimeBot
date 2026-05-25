# Deploy — First Production Deploy (DigitalOcean)

Runbook for the brand-new prod host. Walk top to bottom; the order
matters more than the commands inside each step.

For rolling updates after this initial deploy, use `DEPLOY_APP.md`
(central host) and `DEPLOY_WORKER.md` (regional VPSes).

---

## 0. Accounts and DNS you need first

Have all of these before starting — half of them have ~hour DNS
propagation delays so set them in motion early.

- **DigitalOcean** account, payment method on file. You'll create
  4 droplets (1 app, 3 workers) and 1 Space bucket.
- **Domain registered** — `checkpulse.dev` per these docs. Either
  keep nameservers at the registrar or transfer them to DigitalOcean
  DNS — both work, this runbook assumes registrar-side DNS.
- **Stripe** account, in **live mode** (test mode E2E from
  `LAUNCH_CHECKLIST.md §1` should already be green).
- **Resend** account with `checkpulse.dev` added (verification
  records pending; you'll add them in step 7).
- **GitHub** repo for the app code, with a deploy key or PAT for the
  droplet to clone over.
- An **SSH keypair** on your laptop you'll paste into DigitalOcean.

---

## 1. Provision the app droplet

- Image: Ubuntu 24.04 LTS
- Plan: **Basic / Premium Intel / 2 vCPU / 4 GB / 80 GB SSD**
  (~$24/mo). Postgres, Redis, FastAPI, and four Celery processes
  fit comfortably; downsize after launch if usage is low.
- Region: closest to your main user base.
- Authentication: SSH key (the one you have locally).
- Hostname: `checkpulse-app-prod`.

After it's up, note the public IPv4. Then SSH in as root:

```
ssh root@<droplet-ip>
```

## 2. Harden + install Docker + cloudflared

This runbook uses **Cloudflare Tunnel** for ingress: cloudflared opens
an outbound-only connection to Cloudflare's edge, Cloudflare proxies
inbound traffic to it, and TLS terminates at Cloudflare. The droplet
never opens ports 80/443 to the public internet.

If you'd rather expose the droplet directly with Caddy + Let's Encrypt
TLS, see the Caddy variant in git history (commit `8d97afb`).

```
# Create a non-root user and lock down SSH
adduser --disabled-password --gecos "" checkpulse
usermod -aG sudo checkpulse
mkdir -p /home/checkpulse/.ssh
cp ~/.ssh/authorized_keys /home/checkpulse/.ssh/
chown -R checkpulse:checkpulse /home/checkpulse/.ssh
chmod 700 /home/checkpulse/.ssh && chmod 600 /home/checkpulse/.ssh/authorized_keys

sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh

# Firewall — only SSH needs to be reachable. Cloudflare Tunnel makes
# outbound-only connections; no inbound 80/443 needed on the droplet.
ufw allow OpenSSH
ufw --force enable

# Docker
apt-get update
apt-get install -y docker.io docker-compose-v2 git curl
systemctl enable --now docker
usermod -aG docker checkpulse

# cloudflared (Cloudflare Tunnel daemon)
mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared bookworm main' \
  | tee /etc/apt/sources.list.d/cloudflared.list
apt-get update
apt-get install -y cloudflared
```

Re-SSH as `checkpulse` to confirm key-only auth works. Don't proceed
until you can `ssh checkpulse@<ip>` cleanly — root login is now off.

## 3. Create the Cloudflare Tunnel + DNS records

As `checkpulse` (or root — the credentials end up under whichever user
runs `tunnel login`; pick one and stick with it).

```
# 1. Authenticate cloudflared with your Cloudflare account.
# Prints a URL — open it on your laptop, sign in, pick the zone
# (checkpulse.dev). cloudflared writes ~/.cloudflared/cert.pem.
cloudflared tunnel login

# 2. Create a named tunnel. Generates a UUID and credentials file at
# ~/.cloudflared/<UUID>.json. Save the UUID — you'll need it below.
cloudflared tunnel create checkpulse-prod

# 3. Route DNS — Cloudflare auto-creates CNAME records pointing each
# hostname at <UUID>.cfargotunnel.com. No manual DNS-panel edits.
cloudflared tunnel route dns checkpulse-prod checkpulse.dev
cloudflared tunnel route dns checkpulse-prod app.checkpulse.dev
cloudflared tunnel route dns checkpulse-prod api.checkpulse.dev
```

Verify the records exist:

```
dig +short app.checkpulse.dev
# expect: <something>.cfargotunnel.com.
```

In the Cloudflare dashboard, under **SSL/TLS → Overview**, set the
encryption mode to **Full** (not "Flexible" — the tunnel is already
encrypted end-to-end and "Full" is the right semantic).

## 4. cloudflared config + run as service

As `checkpulse`, replace `<UUID>` below with the tunnel UUID from
step 3:

```
sudo mkdir -p /etc/cloudflared
sudo tee /etc/cloudflared/config.yml <<'EOF'
tunnel: checkpulse-prod
credentials-file: /home/checkpulse/.cloudflared/<UUID>.json

ingress:
  - hostname: api.checkpulse.dev
    service: http://localhost:8000
  - hostname: app.checkpulse.dev
    service: http://localhost:8000
  - hostname: checkpulse.dev
    service: http://localhost:8000
  - service: http_status:404
EOF

sudo cloudflared service install
sudo systemctl enable --now cloudflared
sudo systemctl status cloudflared
# expect: active (running), no errors in journalctl -u cloudflared -n 50
```

Cloudflare manages TLS certs for you — no Let's Encrypt step. If
`cloudflared` fails to connect, the most common cause is a stale or
wrong `credentials-file` path; check `journalctl -u cloudflared -n 100`.

## 5. Clone the repo + create `.env`

```
cd /home/checkpulse
git clone git@github.com:<your-org>/uptimebot.git checkpulse
cd checkpulse
cp .env.example .env
chmod 600 .env
```

Generate secrets locally and paste into `.env`:

```
openssl rand -hex 32   # → SECRET_KEY
openssl rand -hex 32   # → WORKER_SECRET (save this — workers need it)
openssl rand -base64 24 | tr -d '/+=' | head -c 24   # → POSTGRES password
```

Fill in `.env`:

```
DATABASE_URL=postgresql+asyncpg://uptimebot:<pg-pass>@db:5432/uptimebot
DATABASE_URL_SYNC=postgresql://uptimebot:<pg-pass>@db:5432/uptimebot
REDIS_URL=redis://redis:6379/0
SECRET_KEY=<32-hex>
WORKER_SECRET=<32-hex>
TRUST_FORWARDED_FOR=true
APP_ENV=production
APP_URL=https://app.checkpulse.dev
APP_NAME=CheckPulse
# STRIPE_*, RESEND_*, EMAIL_FROM_ADDRESS — filled in steps 6 + 7
```

The committed `docker-compose.yml` hardcodes `POSTGRES_PASSWORD:
localdev` for the `db` service. **Edit that to your generated
password** (or refactor to read `${POSTGRES_PASSWORD}` from `.env`;
either works, hardcoding is fine for a single-host setup).

## 6. Stripe live mode

Walk `BILLING.md §2` — Product/Price creation, customer portal
config, grandfathering decision. End with these values for `.env`:

```
STRIPE_SECRET_KEY=sk_live_…
STRIPE_STARTER_PRICE_ID=price_…   # $12/mo Starter
STRIPE_PRO_PRICE_ID=price_…       # $49/mo Pro
```

Register the webhook *before* setting `STRIPE_WEBHOOK_SECRET` —
Stripe needs the endpoint URL to issue the signing secret:

- Stripe Dashboard → Developers → Webhooks → Add endpoint
- URL: `https://api.checkpulse.dev/billing/webhook`
- Events: `checkout.session.completed`, `customer.subscription.updated`,
  `customer.subscription.deleted`, `invoice.payment_failed`
- Copy the **Signing secret** → `STRIPE_WEBHOOK_SECRET=whsec_…` in `.env`.

## 7. Resend domain verification

Walk `LAUNCH_CHECKLIST.md §2` for the exact SPF / DKIM / DMARC
records. Add them at your DNS registrar. Resend will turn the domain
green on its dashboard within ~10 minutes (sometimes faster).

Copy into `.env`:

```
RESEND_API_KEY=re_…
EMAIL_FROM_ADDRESS=CheckPulse <no-reply@checkpulse.dev>
```

Verify deliverability:

```
dig +short TXT checkpulse.dev | grep spf1
dig +short CNAME resend._domainkey.checkpulse.dev
dig +short TXT _dmarc.checkpulse.dev
```

## 8. First boot

From `/home/checkpulse/checkpulse`:

```
docker compose build
docker compose up -d db redis
# wait ~15s for healthchecks
docker compose run --rm api alembic upgrade head
docker compose up -d api celery-worker celery-alerts-worker celery-beat
docker compose ps
# all five services should be "Up (healthy)" or "Up"
```

Smoke test:

```
curl -fsS https://api.checkpulse.dev/health
# {"status":"ok"}

curl -fsS https://api.checkpulse.dev/healthz/beat
# 503 in the first 15s is normal (beat hasn't written its heartbeat
# yet). After 30s expect 200 with last_seen + age_seconds.
```

Open `https://app.checkpulse.dev` in a browser, register an account,
click the verification email link (lands in your real inbox? if not,
SPF/DKIM/DMARC haven't propagated — wait), sign in. You should land
on `/dashboard`.

## 9. Three regional worker VPSes (Docker / GHCR)

The workers run as a Docker container pulled from GitHub Container
Registry. The `.github/workflows/worker-image.yml` workflow builds and
pushes `ghcr.io/<your-gh-user>/uptimebot-worker:latest` on every push
to `master` that touches `worker/`. Each VPS only needs Docker + a
2-line compose file + a 5-line env file.

(If you'd rather run from a venv + systemd, the artifacts for that
path are still in the repo: `deploy/checkpulse-worker.service` and
`deploy/worker.env.example`. See git history before commit `<this one>`
for the systemd version of this section.)

### Per-VPS setup

For each region (us, eu, asia):

- Provision a small VPS: **1 vCPU / 1 GB / 25 GB SSD** (~$5–6/mo).
  Region = the geographic location; the `REGION` env var below is the
  logical label the central API uses to bucket consensus.
- Hostname: `checkpulse-worker-<region>`.
- OS: Ubuntu 24.04 LTS.

SSH in as root and install Docker:

```
apt-get update
apt-get install -y docker.io docker-compose-v2
systemctl enable --now docker

# Firewall — workers are outbound-only, so just allow SSH.
ufw allow OpenSSH
ufw --force enable
```

Drop the compose file + env file under `/opt/checkpulse`:

```
mkdir -p /opt/checkpulse
cd /opt/checkpulse

# Pull the compose file from the repo (single file — no clone needed).
curl -fsSL https://raw.githubusercontent.com/BetV3/UptimeBot/master/deploy/worker.docker-compose.yml \
  -o docker-compose.yml

# Per-region env file. CHANGE REGION + WORKER_ID per host.
cat > worker.env <<EOF
REGION=us
API_URL=https://api.checkpulse.dev
WORKER_SECRET=<PASTE_SAME_VALUE_AS_CENTRAL_HOST_ENV>
WORKER_ID=checkpulse-us-1
POLL_INTERVAL=10
EOF
chmod 600 worker.env
```

Pull and start the container:

```
docker compose pull
docker compose up -d
docker compose logs -f worker
# expect lines like:
#   Starting worker region=us api=https://api.checkpulse.dev worker_id=checkpulse-us-1
#   No jobs available. Sleeping 10s...
```

Repeat for `eu` and `asia` regions, changing only `REGION` and
`WORKER_ID` in `worker.env`.

### Verify all three workers are heartbeating

On the **central app host**:

```
docker compose exec db psql -U uptimebot -d uptimebot -c \
  "SELECT region, count(*) FROM pending_checks WHERE leased_at > now() - interval '5 minutes' GROUP BY region;"
# expect a non-zero row for each of us, eu, asia within a minute of
# the first worker booting (once you've created at least one monitor)
```

### Rolling updates

When the GitHub Actions workflow pushes a new `:latest` tag (any push
to master that touches `worker/`), redeploy each VPS one at a time:

```
cd /opt/checkpulse
docker compose pull
docker compose up -d
docker image prune -f   # reclaim disk from the old image
```

Do one region at a time so the consensus logic always has at least two
active regions during the rollout.

## 10. DB backups (DigitalOcean Spaces)

Create a Space (`checkpulse-backups`, private, same region as the
droplet). Generate access keys (DO API → Spaces Keys).

On the app host, as `checkpulse`:

```
# Install s3cmd
sudo apt-get install -y s3cmd
s3cmd --configure   # use DO Spaces endpoint and the access keys

# Backup script
sudo tee /usr/local/bin/checkpulse-backup <<'EOF'
#!/bin/bash
set -euo pipefail
ts=$(date -u +%Y%m%d-%H%M%S)
fn=/tmp/checkpulse-${ts}.sql.gz
cd /home/checkpulse/checkpulse
docker compose exec -T db pg_dump -U uptimebot uptimebot | gzip > "$fn"
s3cmd put "$fn" s3://checkpulse-backups/postgres/
rm "$fn"
# Prune local — nothing to do, we deleted /tmp file
# Spaces lifecycle policy (set in DO console) handles old-backup pruning.
EOF
sudo chmod +x /usr/local/bin/checkpulse-backup

# Cron — daily at 04:30 UTC
sudo tee /etc/cron.d/checkpulse-backup <<'EOF'
30 4 * * * checkpulse /usr/local/bin/checkpulse-backup >> /var/log/checkpulse-backup.log 2>&1
EOF
```

In DO Spaces console, set a **lifecycle policy** to delete objects
under `postgres/` after 30 days so storage doesn't grow forever.

**Restore drill** — before launch, do this once on a separate
droplet to prove the backup is restorable:

```
# On a fresh test droplet
s3cmd get s3://checkpulse-backups/postgres/checkpulse-<latest>.sql.gz .
gunzip checkpulse-<latest>.sql.gz
docker run --rm -e POSTGRES_PASSWORD=test -d --name pgrestore -p 5432:5432 postgres:16-alpine
sleep 5
docker exec -i pgrestore psql -U postgres < checkpulse-<latest>.sql
docker exec pgrestore psql -U postgres -d uptimebot -c "SELECT count(*) FROM users;"
# expect non-zero count
docker stop pgrestore
```

## 11. Self-monitoring

Inside CheckPulse, add an HTTP monitor pointed at
`https://api.checkpulse.dev/healthz/beat`:

- `expected_status=200`
- `interval_seconds=60`
- Wire it to your Slack/Discord/email alert channel

This catches a stuck beat process before it silently stops scheduling
checks. Do this once — not every deploy.

## 12. Final verification

Walk `LAUNCH_CHECKLIST.md` end to end. Don't take real customer
money until every box on that file is ticked.

---

## Rollback / blast radius

This is a first deploy, so "rollback" is mostly "tear down":

- App host: `docker compose down -v` wipes containers + volumes.
- Workers: `systemctl stop checkpulse-worker` on each VPS.
- Tunnel: `systemctl stop cloudflared` cuts public ingress immediately
  without touching DNS; `cloudflared tunnel delete checkpulse-prod`
  removes the tunnel and frees the auto-created CNAMEs.
- Stripe: leave the live webhook + products in place; toggle the
  webhook endpoint to disabled if you need to stop incoming events.

For granular rollbacks after the host is live, `DEPLOY_APP.md §10`
covers the migration-aware paths.
