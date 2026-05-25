-- CheckPulse production database sanity queries.
--
-- Run on the app host after deploy:
--     docker compose exec -T db psql -U uptimebot -d uptimebot -f /code/deploy/sanity.sql
--
-- Each query prints its result; the comment above each one states
-- the expectation. Anything outside the expected range = investigate.

\pset border 2

-- ── 1. Every active monitor has next_check_at populated. ─────────────
-- Expect: stale_monitors = 0. A non-zero count means the scheduler
-- has skipped some monitors and they'll never be checked.
SELECT count(*) AS stale_monitors
FROM monitors
WHERE is_active AND next_check_at IS NULL;

-- ── 2. No stuck pending_checks. ──────────────────────────────────────
-- Expect: stuck_pending near 0. The 60s reaper task marks any
-- pending_check whose lease expired > 5 min ago as dead. Persistent
-- non-zero means reap_dead_pending_checks isn't running on celery-beat.
SELECT count(*) AS stuck_pending
FROM pending_checks
WHERE dead = false AND lease_expires_at < now() - interval '5 minutes';

-- ── 3. Per-region recent check throughput (last 5 min). ─────────────
-- Expect: each active region (us, eu, asia) returns > 0. Missing
-- region = that VPS worker is dead or not configured.
SELECT region, count(*) AS recent_checks
FROM checks
WHERE checked_at > now() - interval '5 minutes'
GROUP BY region
ORDER BY region;

-- ── 4. No duplicate OPEN incidents per monitor. ─────────────────────
-- Expect: 0 rows. The advisory lock in finalize_check_result
-- serializes consensus flips per monitor; duplicates here mean the
-- lock isn't being taken (worker rolled back, code reverted, etc.).
SELECT monitor_id, count(*) AS open_count
FROM incidents
WHERE resolved_at IS NULL
GROUP BY monitor_id
HAVING count(*) > 1;

-- ── 5. alert_deliveries state distribution. ─────────────────────────
-- Expect: 'sent' dominates after the first alert fires; 'pending'
-- stays low (< 10) under normal load. Persistent large 'pending' =
-- celery-alerts-worker is down or starved.
SELECT state, count(*) AS rows
FROM alert_deliveries
GROUP BY state
ORDER BY state;

-- ── 6. Recently failed alert deliveries. ────────────────────────────
-- Read last_error to diagnose; these have already exhausted their
-- 4-retry budget. Patterns to watch: 4xx (channel config wrong),
-- timeouts (network), 5xx from the provider (provider degraded).
SELECT id,
       kind,
       attempts,
       left(coalesce(last_error, ''), 100) AS error_excerpt,
       created_at
FROM alert_deliveries
WHERE state = 'failed'
ORDER BY created_at DESC
LIMIT 10;

-- ── 7. Plan + subscription distribution. ────────────────────────────
-- Sanity check: STARTER/PRO rows should have subscription_status
-- 'active' (or 'canceled' with future current_period_end). A FREE
-- user with subscription_status='active' is a bug (webhook race).
SELECT plan,
       subscription_status,
       count(*) AS users
FROM users
GROUP BY plan, subscription_status
ORDER BY plan, subscription_status;

-- ── 8. Email-unverified users older than 24h. ───────────────────────
-- Signups that never confirmed. Some volume is normal (~10–30% of
-- signups bail); a sudden spike suggests deliverability has degraded.
SELECT count(*) AS unverified_24h
FROM users
WHERE NOT email_verified
  AND created_at < now() - interval '24 hours';

-- ── 9. Beat heartbeat freshness via Redis (info only). ──────────────
-- The /healthz/beat endpoint reads this; if you see no row above for
-- recent_checks the beat is likely dead. The endpoint is the
-- authoritative check — see deploy/smoke.sh #2.

-- ── 10. Storage growth — checks table row count and bytes. ──────────
-- Useful to size DB backups. The per-plan retention task should
-- keep this bounded; if it grows monotonically by > 100k/day per
-- 1k monitors something's wrong with cleanup_old_checks.
SELECT count(*) AS check_rows,
       pg_size_pretty(pg_total_relation_size('checks')) AS check_table_size
FROM checks;
