#!/usr/bin/env bash
#
# CheckPulse post-deploy smoke test.
#
# Runs every HTTP-level check from LAUNCH_CHECKLIST in one pass.
# Run from your laptop after DEPLOY_FIRST.md finishes — exits 0 if
# the host is healthy, non-zero with a specific failure on first
# broken check.
#
# Usage:
#     ./deploy/smoke.sh                              # against api.checkpulse.dev
#     ./deploy/smoke.sh api.checkpulse.dev           # explicit api host
#     ./deploy/smoke.sh api.example.com app.example.com   # override both
#
# Requires: curl. Optional: hey (for the rate-limit check; if absent,
# the script does a slower 70x curl loop instead).

set -euo pipefail

API_HOST="${1:-api.checkpulse.dev}"
APP_HOST="${2:-${API_HOST/api./app.}}"

pass() { printf '\033[32m✓\033[0m %s\n' "$1"; }
fail() { printf '\033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }
info() { printf '· %s\n' "$1"; }

echo "Smoke testing https://${API_HOST} (app: https://${APP_HOST})"
echo

# ── 1. Basic liveness ────────────────────────────────────────────────
curl -fsS "https://${API_HOST}/health" >/dev/null \
    || fail "/health did not return 200"
pass "/health → 200"

# ── 2. Beat heartbeat (allow up to 30s for first heartbeat) ─────────
info "Waiting up to 30s for /healthz/beat to go green…"
ok=0
for _ in 1 2 3 4 5 6; do
    if curl -fsS "https://${API_HOST}/healthz/beat" >/dev/null 2>&1; then
        ok=1; break
    fi
    sleep 5
done
[ "$ok" = "1" ] || fail "/healthz/beat never returned 200 — celery-beat may not be running"
pass "/healthz/beat → 200"

# ── 3. Legal + public pages ──────────────────────────────────────────
for path in / /legal/privacy /legal/terms /docs/getting-started; do
    curl -fsS "https://${APP_HOST}${path}" >/dev/null \
        || fail "${path} did not return 200"
    pass "${path} → 200"
done

# ── 4. TLS + security headers ────────────────────────────────────────
hdrs=$(curl -fsSI "https://${APP_HOST}/")
echo "$hdrs" | grep -qi "^strict-transport-security:" \
    || fail "HSTS header missing — TransportMiddleware not enabled, APP_ENV != production, or proxy stripped it"
pass "HSTS header present"

echo "$hdrs" | grep -qi "^x-content-type-options: *nosniff" \
    || fail "X-Content-Type-Options missing"
pass "X-Content-Type-Options: nosniff"

# ── 5. Stripe webhook rejects bad signatures ─────────────────────────
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "https://${API_HOST}/billing/webhook" \
    -H "stripe-signature: t=0,v1=bogus" -d '{}')
[ "$code" = "400" ] \
    || fail "/billing/webhook with bad signature returned ${code}, expected 400"
pass "/billing/webhook rejects bad signature → 400"

# ── 6. Internal endpoints reject bad worker secret ──────────────────
code=$(curl -s -o /dev/null -w "%{http_code}" "https://${API_HOST}/internal/jobs?region=us" \
    -H "X-Worker-Secret: bogus" -H "X-Worker-ID: smoke")
[ "$code" = "403" ] \
    || fail "/internal/jobs with bad secret returned ${code}, expected 403"
pass "/internal/jobs rejects bad worker secret → 403"

# ── 7. JWT cookie posture on login form GET ─────────────────────────
# Visiting the login page should NOT set a session cookie. After a
# (failed) login POST, the response cookies — if any — must be
# Secure and SameSite=Lax.
login_hdrs=$(curl -s -i -X POST "https://${APP_HOST}/dashboard/login" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "email=smoke@test.invalid&password=x" 2>/dev/null | head -50)
if echo "$login_hdrs" | grep -qi "^set-cookie:"; then
    echo "$login_hdrs" | grep -i "^set-cookie:" | while read -r line; do
        echo "$line" | grep -qi "secure" \
            || fail "Auth cookie set without Secure flag — APP_ENV likely != production"
        echo "$line" | grep -qi "samesite=lax" \
            || fail "Auth cookie missing SameSite=Lax"
    done
    pass "Auth cookies Secure + SameSite=Lax"
else
    info "(no Set-Cookie on failed login — expected; nothing to check)"
fi

# ── 8. Rate limiter kicks in ────────────────────────────────────────
# slowapi default is 60/minute. Hammer /auth/login with > 60 requests
# and expect at least one 429. If TRUST_FORWARDED_FOR is off, the
# limiter sees the proxy IP and ALL requests get one bucket — still
# fires. If the limiter is broken, none of them will 429.
info "Hammering /auth/login to verify rate limiter (~30s)…"
got_429=0
for i in $(seq 1 75); do
    code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "https://${API_HOST}/auth/login" \
        -H "Content-Type: application/x-www-form-urlencoded" \
        -d "email=smoke@test.invalid&password=x")
    if [ "$code" = "429" ]; then got_429=1; break; fi
done
[ "$got_429" = "1" ] \
    || fail "Rate limiter never returned 429 in 75 requests — slowapi may be misconfigured"
pass "Rate limiter → 429 within 75 requests"

# ── 9. Email DNS (run client-side via dig if available) ─────────────
if command -v dig >/dev/null 2>&1; then
    domain="${EMAIL_DOMAIN:-${APP_HOST#app.}}"
    spf=$(dig +short TXT "$domain" | grep "spf1" || true)
    [ -n "$spf" ] && pass "SPF record present at ${domain}" || \
        fail "No SPF record at ${domain} — Resend domain unverified"

    dmarc=$(dig +short TXT "_dmarc.${domain}" || true)
    [ -n "$dmarc" ] && pass "DMARC record present at _dmarc.${domain}" || \
        info "(no DMARC record yet — non-blocking, but add per LAUNCH_CHECKLIST §2)"
else
    info "(dig not installed — skip DNS checks; run manually per LAUNCH_CHECKLIST §2)"
fi

echo
printf '\033[32mAll smoke checks passed.\033[0m\n'
echo
echo "Next:"
echo "  - Run deploy/sanity.sql on the host:"
echo "      docker compose exec -T db psql -U uptimebot -d uptimebot -f /code/deploy/sanity.sql"
echo "  - Load smoke (optional): deploy/load.md"
echo "  - Walk LAUNCH_CHECKLIST.md sections 1, 3 (Stripe E2E + alert smoke — manual)"
