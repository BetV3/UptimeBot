# Load smoke test

Run from your laptop after `deploy/smoke.sh` is green. This is
**not** real load testing — it's a single-machine sanity pass to
catch obvious capacity issues before you start onboarding users.

## Install `hey`

```bash
# macOS
brew install hey

# Linux (download static binary)
wget https://hey-release.s3.us-east-2.amazonaws.com/hey_linux_amd64 -O /usr/local/bin/hey
chmod +x /usr/local/bin/hey
```

## 1. App liveness under load

5,000 requests, 50 concurrent. `/health` is a constant-time endpoint
with no DB hit — should sustain hundreds of RPS on a 2-vCPU droplet.

```bash
hey -n 5000 -c 50 https://api.checkpulse.dev/health
```

**Pass criteria:**
- p99 < 500ms
- 0 non-200 responses
- "Requests/sec" > 100

If p99 spikes or you see 5xx, the bottleneck is almost always the
proxy → uvicorn worker count. Bump uvicorn's `--workers` in
`docker-compose.yml` (default is 1 with `--reload`; for prod use
`--workers 4` and drop `--reload`).

## 2. Auth-path load (rate-limited)

Hit `/auth/login` past the 60/min limiter. Expect a mix of 401
(invalid credentials) and 429 (rate-limited). The point is to
confirm the limiter scales — it shouldn't bring the whole API down.

```bash
hey -n 1000 -c 20 -m POST \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "email=loadtest@test.invalid&password=x" \
    https://api.checkpulse.dev/auth/login
```

**Pass criteria:**
- p99 < 1s (slowapi is in-process, should be fast even when limiting)
- Status distribution: mostly 401 + 429, **no 500s**

## 3. Worker pipeline load (synthetic)

Once a few real monitors are configured, the realistic load is
"N monitors × 1/interval requests/sec" per region. For a soft cap
check, simulate by creating a project with 100 monitors at 60s
interval (each region polls every ~10s, so ~10 jobs/sec per region):

```bash
# Run on the app host
docker compose exec -T db psql -U uptimebot -d uptimebot <<'SQL'
-- Confirm scheduler keeps up
SELECT region,
       max(scheduled_at) AS last_scheduled,
       count(*) FILTER (WHERE leased_at IS NOT NULL) AS leased,
       count(*) FILTER (WHERE dead = false AND leased_at IS NULL) AS queued
FROM pending_checks
GROUP BY region
ORDER BY region;
SQL
```

**Pass criteria:**
- `queued` stays under 50 per region (workers are keeping up)
- `last_scheduled` is fresh (< 60s old)

Persistent backlog = either the regional worker is undersized, or
the API is slow to serve `/internal/jobs`. Check
`docker compose logs api` for slow-query patterns.

## What this load test does NOT cover

- **Multi-day soak**: memory leaks and connection pool exhaustion
  only show under sustained load. Re-run after 24h on a real prod
  install.
- **Realistic write-amplification**: the worker writes 1 row per
  check + occasional incident/delivery rows. At 1k monitors × 60s
  interval that's ~17 inserts/sec sustained — well within
  Postgres on a 2-vCPU droplet, but check `pg_stat_io` periodically.
- **Cold cache**: every `hey` run heats the OS page cache and pgbouncer
  pool. Real-world first-request latency is higher.

For real load testing before you outgrow a single host, use k6 or
locust with a realistic mix and a separate origin machine.
