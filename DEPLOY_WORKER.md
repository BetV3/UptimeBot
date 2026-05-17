# Deploy — VPS Worker Node

Runbook for the regional check workers (one per region: us, eu, asia).
These are standalone Python processes running `worker/run.py`; they
don't run Docker, Postgres, or Celery.

Each VPS is independent — redeploy them one at a time so at most one
region is offline during the upgrade. The lease system in the API
tolerates a missing region; just don't take all three down at once.

> The central app host must be upgraded **first**. See `DEPLOY_APP.md`.
> An old worker against a new API is fine (the API defaults
> `X-Worker-ID` to `"unknown"` if missing). A new worker against an old
> API will error on startup or submit results that the old API doesn't
> understand — don't do it.

---

## 0. Prerequisites

- You are SSH'd into one of the VPS workers (start with whichever
  region has the lightest traffic).
- The central app host has already been upgraded and is passing
  `/healthz/beat`.
- You know the checkout path. Typical layouts:
  - `/opt/checkpulse/worker` — systemd or manual install
  - `~/UptimeBot/worker` — dev-style checkout under a regular user

## 1. Pull code

```
cd /opt/checkpulse   # adjust to your path
git fetch --all
git checkout <tag-or-sha>   # same ref you deployed on the app host
git pull --ff-only
```

Nothing in `worker/requirements.txt` changed this round (still just
`httpx>=0.28.0`), so a reinstall isn't strictly required. If your
venv is old enough that httpx is pre-0.28, refresh it:

```
/opt/checkpulse/venv/bin/pip install -r worker/requirements.txt
```

## 2. Set WORKER_ID (optional but recommended)

The new lease system tags each `pending_check` with the worker that
claimed it, so stale submissions from a reclaimed lease can't wipe
another worker's in-flight row. Default is `platform.node()`.

If your VPS hostnames are unique and meaningful (e.g. `us-1.prod`),
you can skip this. If they're generic cloud-provider strings
(`ip-10-0-0-42`), set it explicitly so incident postmortems are
readable.

Edit the env file the worker reads:

```
# systemd-managed worker
sudo $EDITOR /etc/checkpulse/worker.env
# add or uncomment:
# WORKER_ID=checkpulse-us-1

# manually-managed worker (screen/tmux/supervisor)
# add WORKER_ID to however you're setting REGION/API_URL/WORKER_SECRET
```

## 3. Restart the worker

### If using the systemd unit (C4)

```
sudo systemctl restart checkpulse-worker
sudo systemctl status checkpulse-worker   # should be active (running)
journalctl -u checkpulse-worker -f --since "1 minute ago"
```

### If running manually under screen/tmux/supervisor

Kill the old process and start the new one:

```
# find it
pgrep -af run.py

# stop it — ctrl-C in the screen session, or:
kill <pid>

# restart — example, adjust to your setup
cd /opt/checkpulse
REGION=us \
API_URL=https://api.checkpulse.example.com \
WORKER_SECRET=... \
WORKER_ID=checkpulse-us-1 \
./venv/bin/python worker/run.py
```

## 4. Verify

Within ~15s of restart you should see:

```
Worker starting: id=checkpulse-us-1 region=us api=... poll=10s
Got N job(s)
Posted N result(s)
  [+] abc12345... up 142ms
```

On the app host, confirm this VPS is claiming pending_checks with its
new `worker_id`:

```
# run on app host
docker compose exec db psql -U uptimebot -d uptimebot -c \
  "SELECT region, worker_id, count(*) FROM pending_checks
   WHERE dead = false AND leased_at > now() - interval '2 minutes'
   GROUP BY region, worker_id ORDER BY region;"
```

Your just-restarted VPS's `WORKER_ID` should appear in its region's row.

Also confirm heartbeats are landing:

```
# run on app host
curl -fsS -H "X-Worker-Secret: $WORKER_SECRET" \
  https://<api>/internal/workers | jq
# expect a healthy row for this region with stale: false
```

## 5. Move to the next region

Wait ~2 minutes after the first VPS is verified healthy before touching
the next region. This gives time for the A2 lease system to cycle
through one full polling round and confirm nothing is wedged.

Repeat steps 1–4 on each remaining VPS. Do **not** parallelize — one
region at a time.

## 6. First-time systemd install (C4)

Only do this if the VPS isn't already running `worker/run.py` under
systemd. Everything below is one-time setup; subsequent deploys follow
steps 1–4 above.

```
# 6a. Service user + directories
sudo useradd -r -s /sbin/nologin checkpulse
sudo mkdir -p /opt/checkpulse /var/log/checkpulse /etc/checkpulse
sudo chown checkpulse:checkpulse /var/log/checkpulse

# 6b. Drop the checkout
sudo git clone <repo-url> /opt/checkpulse
sudo chown -R checkpulse:checkpulse /opt/checkpulse

# 6c. Python venv
sudo -u checkpulse python3 -m venv /opt/checkpulse/venv
sudo -u checkpulse /opt/checkpulse/venv/bin/pip install \
  -r /opt/checkpulse/worker/requirements.txt

# 6d. Env file (chmod 600, root:checkpulse)
sudo cp /opt/checkpulse/deploy/worker.env.example /etc/checkpulse/worker.env
sudo chown root:checkpulse /etc/checkpulse/worker.env
sudo chmod 600 /etc/checkpulse/worker.env
sudo $EDITOR /etc/checkpulse/worker.env   # fill in REGION, API_URL,
                                          # WORKER_SECRET, WORKER_ID

# 6e. Install and start the service
sudo cp /opt/checkpulse/deploy/checkpulse-worker.service \
  /etc/systemd/system/checkpulse-worker.service
sudo systemctl daemon-reload
sudo systemctl enable --now checkpulse-worker
sudo systemctl status checkpulse-worker
journalctl -u checkpulse-worker -f
```

If you previously ran the worker under screen/tmux/supervisor, kill
that process before enabling the systemd unit — two copies will fight
over the same leases (not destructive, but wasteful).

## 7. Rollback

Per-VPS, fast and safe:

```
cd /opt/checkpulse
git checkout <previous-tag>
sudo systemctl restart checkpulse-worker
# or, if manual: kill + relaunch with the old code
```

The worker protocol is forward-compatible — an old `run.py` works
against the new API. The only thing you lose by reverting is the hard
timeout and the retry-once behavior added in A3.

If rolling back the API too: revert workers **after** reverting the
API. Same "app first, workers second" ordering as the forward deploy.
