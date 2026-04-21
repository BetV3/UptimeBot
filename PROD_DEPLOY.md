# Prod Deploy Checklist — Worker Pipeline Rework

Running list of what has to happen on prod as the WORKER.md phases land.
Work through phases in order; don't skip ahead.

---

## Phase A1 — `next_check_at` on monitors

**Migration:** `b4c5d6e7f8a9_add_next_check_at_to_monitors`

Run on central/app node (where Postgres lives):
```
alembic upgrade head
```

Redeploy:
- [ ] API (reads/writes `monitors.next_check_at` via `/internal/results`, `POST /projects/{id}/monitors`, `POST /monitors/{id}/resume`, dashboard equivalents)
- [ ] `celery-worker` (new scheduler logic in `app/workers/tasks.py`)
- [ ] `celery-beat` (unchanged schedule but bundled with the worker image)

No VPS worker change required for A1.

Post-deploy verification:
- `SELECT name, is_active, next_check_at FROM monitors ORDER BY name;` — every active monitor should have `next_check_at` populated.
- Watch `pending_checks` over 2 minutes: new rows appear for due monitors.
- `SELECT count(*) FROM checks WHERE checked_at > now() - interval '5 minutes';` — should be > 0 if at least one region's worker is healthy.

---

## Phase A2 — lease-ified `pending_checks`

**Migration:** `c5d6e7f8a9b0_leaseify_pending_checks`

Run on central/app node:
```
alembic upgrade head
```

The migration:
- Adds lease columns + unique partial index `(monitor_id, region) WHERE dead = false`.
- Backfills existing `claimed_at IS NOT NULL` rows as `dead = true` (they already had results).
- Backfills unclaimed rows older than 10 min as `dead = true`.
- Drops `claimed_at`.

**Deploy order matters** — the API must be updated before the VPS workers, otherwise old workers POSTing /results without `X-Worker-ID` will silently leave pending_checks un-deleted (sweeper will catch them after 10 min, but it's wasteful).

Redeploy:
- [ ] API first (accepts optional `X-Worker-ID`; defaults to `"unknown"` so old workers don't 400)
- [ ] `celery-worker` (new `reap_dead_pending_checks` task + scheduler changes)
- [ ] `celery-beat` (picks up the new 60s `reap-dead-pending-checks` schedule)
- [ ] VPS worker (us) — restart with new `worker/run.py`
- [ ] VPS worker (eu) — restart with new `worker/run.py`
- [ ] VPS worker (asia) — restart with new `worker/run.py`

Env var additions on each VPS worker:
- Optional: `WORKER_ID=<hostname-or-region-tag>` (defaults to `platform.node()`; set explicitly if hostnames aren't unique/meaningful).

Post-deploy verification:
- `SELECT region, count(*) FILTER (WHERE leased_at IS NOT NULL) AS leased, count(*) FILTER (WHERE dead) AS dead FROM pending_checks GROUP BY region;`
- Watch for stuck rows: `SELECT count(*) FROM pending_checks WHERE dead = false AND lease_expires_at < now() - interval '5 minutes';` should stay near zero.
- `SELECT * FROM pending_checks ORDER BY scheduled_at DESC LIMIT 10;` — `worker_id` should be populated after workers restart.

Rollback: the migration has a functional `downgrade()`. Don't run it after new rows have been written with `attempts > 0` (the column is dropped; history is lost).

---

## Phase A3 — retries + hard timeout in the worker

**No migration.**

Redeploy:
- [ ] VPS worker (us)
- [ ] VPS worker (eu)
- [ ] VPS worker (asia)

Central/app nodes unchanged.

Post-deploy verification:
- Intentionally knock one region's target off (e.g., block a URL in firewall) and watch `checks.error` — you should see the final `"down"` row with a descriptive error; the retry attempt is not individually logged.
- Point a monitor at a URL that hangs (e.g., `https://httpbin.org/delay/100`) with `timeout_seconds = 5`. Expected: check returns DOWN within ~15s (timeout + retry + backoff + slack), not stuck forever.

---

## Phase B1+B2 — durable alert deliveries on a dedicated Celery queue

**Migration:** `d6e7f8a9b0c1_add_alert_deliveries_table`

Creates `alert_deliveries (id, incident_id, alert_channel_id, kind, state,
attempts, last_error, created_at, sent_at)` with a unique index on
`(incident_id, alert_channel_id, kind)` — that triple is the idempotency
key and prevents duplicate alerts if `/internal/results` runs twice.

Run on central/app node:
```
alembic upgrade head
```

What changes in code:
- `app/services/alerts.py` — senders converted to sync (`httpx.Client`). A
  new `enqueue_alerts()` helper inserts `alert_deliveries` rows with
  `ON CONFLICT DO NOTHING` and returns new IDs. The old async
  `dispatch_alerts` is gone; `send_test_alert` → `send_test_alert_sync`
  wrapped in `asyncio.to_thread(...)` at the HTTP handlers.
- `/internal/results` now calls `enqueue_alerts`, commits the DB
  transaction, then fires `send_alert_delivery.delay(id)` for each new
  delivery. Commit-then-enqueue ordering matters — don't reorder.
- New Celery task `app.workers.tasks.send_alert_delivery` routed to the
  `alerts` queue. `max_retries=4`, exponential backoff, capped at 600s.
- `docker-compose.yml` — new `celery-alerts-worker` service running
  `celery worker -Q alerts --concurrency=4`. The existing `celery-worker`
  now explicitly runs `-Q default` so it can't starve on slow webhooks.

**Deploy order matters.** The migration must be applied before any API
node that calls `enqueue_alerts`, and the alerts worker must be running
before the API starts dispatching. If the alerts worker is lagging or
missing, the rows accumulate with `state='pending'` and send whenever it
comes up — nothing is lost, just delayed.

Redeploy:
- [ ] Run migration on db
- [ ] Start `celery-alerts-worker` (new service)
- [ ] Redeploy `celery-worker` (now queue-scoped to `default`)
- [ ] Redeploy `celery-beat` (schedule unchanged but bundled with the image)
- [ ] Redeploy API last

No VPS worker change required for B1+B2.

Post-deploy verification:
- `SELECT state, count(*) FROM alert_deliveries GROUP BY state;` — after a
  real or simulated down→up cycle, rows should transition to `sent`
  within seconds.
- `SELECT id, state, attempts, last_error FROM alert_deliveries WHERE
  state != 'sent' ORDER BY created_at DESC LIMIT 20;` — investigate any
  `pending` rows older than a minute or any `failed` rows.
- Trigger a test alert from the dashboard; `/alerts/{id}/test` path still
  works (it bypasses the queue and sends synchronously via
  `asyncio.to_thread`).
- Check Celery logs on `celery-alerts-worker` — you should see
  `send_alert_delivery` tasks succeeding.

Rollback:
- The downgrade drops `alert_deliveries` entirely. Before downgrading,
  revert the API + workers to the pre-B1 image or alerts will error
  trying to insert into a missing table.
- An intermediate rollback is: leave the table in place, revert the code.
  Orphan rows sit in `pending` but do nothing.

---

## Phase B3 — advisory-lock'd `finalize_check_result`

**No migration.**

Moves the consensus rollup + incident management + alert enqueue out of
`/internal/results` into a dedicated Celery task
`app.workers.tasks.finalize_check_result`. The task takes
`pg_advisory_xact_lock(hashtext(monitor_id::text))` inside its
transaction, which serializes concurrent finalize calls for the same
monitor. This fixes the last known race: two regions posting near-
simultaneously can no longer both drive the consensus flip and double-
write an incident.

What changes in code:
- `app/services/alerts.py` — replaced async `enqueue_alerts` with
  `enqueue_alerts_sync(session, ...)`. API layer no longer calls it
  directly; only the Celery task does.
- `app/workers/tasks.py` — new `finalize_check_result(monitor_id)`.
  Advisory lock → latest-check-per-region consensus → UP/DOWN flip →
  maybe new incident or resolve open one → `enqueue_alerts_sync`. Commit
  releases the lock. After commit, dispatches
  `send_alert_delivery.delay(id)` per new delivery.
- `app/api/routes/internal.py` — `/internal/results` now:
  commits check results + pending_check deletes + `last_checked_at`
  update, THEN fires `finalize_check_result.delay(monitor_id)` for each
  unique monitor in the batch. `_update_monitor_status` helper and the
  async `enqueue_alerts` import are removed.

Deploy order: API and celery-worker must ship together. If the API ships
first pointing at a worker image without `finalize_check_result`,
dispatches sit in Redis until the worker comes up (no data loss, but
monitor statuses / alerts are delayed). If the worker ships first, it
idles waiting for tasks — safe. Preferred:

- [ ] Redeploy `celery-worker` (adds `finalize_check_result` task)
- [ ] Redeploy `celery-alerts-worker` (unchanged image, but restart so it
      loads the new code on next worker boot)
- [ ] Redeploy `celery-beat` (unchanged schedule)
- [ ] Redeploy API last

No VPS worker change required for B3.

Post-deploy verification:
- `SELECT id, current_status, last_checked_at FROM monitors ORDER BY
  last_checked_at DESC LIMIT 20;` — statuses should continue flipping
  (watch over a few minutes). A stale `last_checked_at` means
  `/internal/results` is failing; a fresh `last_checked_at` with a
  wrong-looking `current_status` means finalize is failing — check
  celery-worker logs.
- `SELECT count(*) FROM celery_taskmeta WHERE task_name =
  'app.workers.tasks.finalize_check_result';` (if result backend is
  wired up) should grow.
- Celery worker logs should show `finalize_check_result` runs with
  `{deliveries: N}` output on transitions.
- Force a concurrent-transition test: `pg_sleep()` inside the task
  during acceptance testing, or just watch for duplicate incidents in
  prod: `SELECT monitor_id, count(*) FROM incidents WHERE started_at >
  now() - interval '1 hour' AND resolved_at IS NULL GROUP BY monitor_id
  HAVING count(*) > 1;` — should always be empty.

Rollback: code-only revert. No schema changes to undo.

---

## Phase C — Ops hardening

**No migration.**

Four independent pieces; any one can ship without the others.

### C1 — Redis AOF + persistent volume

In `docker-compose.yml` Redis now runs with
`--appendonly yes --appendfsync everysec` and mounts a named volume at
`/data`. This means a hard Redis restart only loses ~1s of broker state
instead of everything in flight.

**Migration note for existing prod Redis**: the first boot after enabling
AOF rewrites the on-disk format. Bring the service down, start it with
the new command, and watch `redis-cli info persistence` for
`aof_enabled:1`. Celery brokers don't tolerate Redis downtime well —
expect `celery-worker` to log reconnection errors during the restart.
Existing queued tasks already in memory survive if you do a graceful
stop, but a crash-restart without AOF has always been lossy anyway.

### C2 — Flower with basic auth

New `flower` service in docker-compose on port 5555. Requires
`FLOWER_BASIC_AUTH=user:password` in `.env.local` (Flower reads it
automatically — env var name is fixed). Front this with Caddy/nginx
TLS in prod; don't expose port 5555 to the public internet unless you
trust basic auth alone.

- [ ] Add `FLOWER_BASIC_AUTH` to `.env.local` on the app host
- [ ] `docker compose up -d flower`
- [ ] Point an internal subdomain (e.g. `flower.checkpulse.internal`) at
      the app host via reverse proxy

### C3 — Beat liveness heartbeat

`schedule_pending_checks` now writes
`checkpulse:beat:last_seen = <unix ts>` to Redis with a 300s TTL after
every successful run. New endpoint `GET /healthz/beat` on the API
returns 200 if the key is fresh (< 90s old) and 503 otherwise.

Wire this into an external uptime monitor (CheckPulse can monitor
itself — add an HTTP monitor for
`https://api.checkpulse.example.com/healthz/beat` with
`expected_status=200`, 60s interval). Alerts fire if the beat process
dies or the scheduler task starts failing.

No deploy gating — this endpoint returns 503 when first shipped until
beat writes its first heartbeat (within 15s of starting). Don't page on
that.

### C4 — systemd unit for `worker/run.py`

Unit file at `deploy/checkpulse-worker.service`, env template at
`deploy/worker.env.example`.

First-time install on each VPS:
1. Create service user: `useradd -r -s /sbin/nologin checkpulse`
2. `mkdir -p /opt/checkpulse /var/log/checkpulse /etc/checkpulse`
3. `chown checkpulse:checkpulse /var/log/checkpulse`
4. Sync the checkout to `/opt/checkpulse/worker` and
   `python -m venv /opt/checkpulse/venv &&
    /opt/checkpulse/venv/bin/pip install -r worker/requirements.txt`
5. Copy `deploy/worker.env.example` to `/etc/checkpulse/worker.env`,
   fill in secrets, `chmod 600`, `chown root:checkpulse`
6. `cp deploy/checkpulse-worker.service /etc/systemd/system/`
7. `systemctl daemon-reload && systemctl enable --now checkpulse-worker`

Tail logs: `journalctl -u checkpulse-worker -f`. Restart on deploy:
`systemctl restart checkpulse-worker`.

Rollback: `systemctl stop checkpulse-worker`, run the previous
`run.py` manually under `screen`/`tmux` while you debug. The unit is
safe to leave installed even when stopped.
