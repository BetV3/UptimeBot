# Deploy — Central App Host

Runbook for deploying the A+B+C changes on the host that runs `db`,
`redis`, `api`, `celery-*`, and `flower`. Follow top to bottom — the
order between steps matters more than the commands inside each step.

> For a conceptual explanation of what each phase does, see
> `PROD_DEPLOY.md`. This file is the how.

---

## 0. Prerequisites

- You are SSH'd into the host that owns `docker-compose.yml`.
- Docker + Docker Compose v2 installed.
- `.env` and `.env.local` already exist (used by existing services).
- You know which commit/tag you're deploying.
- You have a recent Postgres backup — the A2 migration drops the
  `claimed_at` column after backfilling, which is a one-way change.

## 1. Pull code

```
cd /opt/checkpulse  # or wherever the checkout lives
git fetch --all
git checkout <tag-or-sha>
git pull --ff-only
```

## 2. Update env vars

Edit `.env.local` and add:

```
# Phase C2 — only required if you're enabling Flower.
FLOWER_BASIC_AUTH=admin:<strong-random-password>
```

Nothing else in `.env` / `.env.local` needs to change. `WORKER_SECRET`
must remain identical to what the VPS workers send — rotating it is a
separate, coordinated change.

## 3. Back up the database

```
docker compose exec -T db pg_dump -U uptimebot uptimebot \
  | gzip > /var/backups/checkpulse-$(date +%Y%m%d-%H%M).sql.gz
```

Verify the file isn't zero bytes before proceeding.

## 4. Rebuild the image

`requirements.txt` gained `flower==2.0.1`; the image needs a rebuild
even if you're not enabling Flower so the Python environment is
consistent across services.

```
docker compose build
```

## 5. Run migrations

Three new migrations land in one go:

- `b4c5d6e7f8a9_add_next_check_at_to_monitors` (A1)
- `c5d6e7f8a9b0_leaseify_pending_checks` (A2)
- `d6e7f8a9b0c1_add_alert_deliveries_table` (B1)

```
docker compose run --rm api alembic upgrade head
```

Expected output ends with three `Running upgrade ... -> ...` lines and
no tracebacks.

Spot-check:

```
docker compose exec db psql -U uptimebot -d uptimebot -c \
  "\d pending_checks"
# should show leased_at / lease_expires_at / worker_id / attempts / dead
#         and no claimed_at

docker compose exec db psql -U uptimebot -d uptimebot -c \
  "\d alert_deliveries"
# should exist
```

## 6. Redis: enable AOF (one-time)

The compose file now starts Redis with
`--appendonly yes --appendfsync everysec` and mounts a `redisdata`
volume. First boot rewrites the on-disk format.

```
docker compose stop redis
docker compose up -d redis
# wait ~5s for it to come up
docker compose exec redis redis-cli INFO persistence | grep aof_enabled
# expect: aof_enabled:1
```

Celery workers will log reconnection errors for the few seconds Redis
is down — harmless, ignore. If you see `aof_enabled:0`, the compose
change didn't land — re-check step 1.

## 7. Restart services — order matters

Do these one at a time. Wait for each to reach a healthy state before
moving on (check `docker compose ps` + a quick log tail).

```
# 7a. default-queue Celery worker — now scoped to -Q default and
# carries the new tasks (reap, finalize, send_alert_delivery).
docker compose up -d celery-worker

# 7b. alerts-queue Celery worker — brand new service. If you skip this,
# alert_deliveries rows will accumulate as 'pending' and no one will
# send them.
docker compose up -d celery-alerts-worker

# 7c. beat — unchanged schedule but must load the new task module.
docker compose up -d celery-beat

# 7d. Flower — optional. Skip if you didn't set FLOWER_BASIC_AUTH.
docker compose up -d flower

# 7e. API last. If API ships before the workers, dispatched tasks just
# sit in Redis until the workers catch up (not destructive, just a
# window of delayed status updates).
docker compose up -d api
```

## 8. Verify

Run each query and check the output matches the expectation.

```
# A1 — every active monitor has next_check_at populated.
docker compose exec db psql -U uptimebot -d uptimebot -c \
  "SELECT count(*) FROM monitors WHERE is_active AND next_check_at IS NULL;"
# expect: 0

# A2 — leases and worker_id present on in-flight pending_checks.
docker compose exec db psql -U uptimebot -d uptimebot -c \
  "SELECT region, count(*) FILTER (WHERE leased_at IS NOT NULL) AS leased
   FROM pending_checks WHERE dead = false GROUP BY region;"
# expect: non-zero 'leased' counts in each active region within ~30s

# A3 — no stuck rows.
docker compose exec db psql -U uptimebot -d uptimebot -c \
  "SELECT count(*) FROM pending_checks
   WHERE dead = false AND lease_expires_at < now() - interval '5 minutes';"
# expect: 0 (stays near 0 as time passes)

# B1 — after a real or simulated down->up cycle, deliveries transition.
docker compose exec db psql -U uptimebot -d uptimebot -c \
  "SELECT state, count(*) FROM alert_deliveries GROUP BY state;"
# expect: 'sent' grows; 'pending' stays small; 'failed' is diagnostic.

# B3 — no duplicate open incidents (the advisory lock guarantee).
docker compose exec db psql -U uptimebot -d uptimebot -c \
  "SELECT monitor_id, count(*) FROM incidents
   WHERE resolved_at IS NULL GROUP BY monitor_id HAVING count(*) > 1;"
# expect: 0 rows, always.

# C3 — beat liveness.
curl -fsS http://localhost:8000/healthz/beat
# expect: {"status":"ok","last_seen":...,"age_seconds":<90}
# 503 within the first 15s is normal; investigate if it persists.
```

Tail the alerts worker for 60s and confirm you see task activity:

```
docker compose logs -f --tail=50 celery-alerts-worker
```

## 9. Wire up self-monitoring (once)

Add an HTTP monitor inside CheckPulse pointed at
`https://<your-api>/healthz/beat` with:
- `expected_status=200`
- `interval_seconds=60`

This catches a stuck beat process before it silently stops scheduling
checks. Do this exactly once — not every deploy.

## 10. Rollback

The worst-case escape hatch, in decreasing order of preference:

1. **Code-only revert** (covers B3, C2, C3):
   `git checkout <previous-tag> && docker compose build && redeploy
   in the same order as step 7.` No schema changes to undo.

2. **Revert B1** (alert_deliveries):
   `docker compose run --rm api alembic downgrade c5d6e7f8a9b0`
   then code-revert. The downgrade drops the table; orphaned Celery
   `send_alert_delivery` tasks in Redis will fail fast on the next
   worker pickup — safe.

3. **Revert A2** (pending_checks leases):
   Avoid if possible. The downgrade re-creates `claimed_at` and copies
   `leased_at` into it, but any row with `attempts > 0` loses that
   history. If you must: stop all VPS workers first, then downgrade,
   then code-revert, then restart workers.

4. **Revert A1** (`next_check_at`):
   `alembic downgrade <revision-before-b4c5d6e7f8a9>`. Drops the column;
   safe to re-apply later.

Redis AOF is safe to leave on after a rollback — the old code works
fine against an AOF-enabled broker.
