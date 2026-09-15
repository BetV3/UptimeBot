#!/usr/bin/env bash
# Boot the real app with the REAL production Turnstile keys and verify
# both the favicon wiring and the signup gate over HTTP.
set -u
cd /home/bet/checkpulse-ai/repo

docker rm -f cp-live-pg >/dev/null 2>&1
docker run -d --name cp-live-pg -e POSTGRES_PASSWORD=e2e -e POSTGRES_USER=e2e -e POSTGRES_DB=e2e \
  -p 55434:5432 postgres:16-alpine >/dev/null
for i in $(seq 1 40); do docker exec cp-live-pg pg_isready -U e2e >/dev/null 2>&1 && break; sleep 1; done

export APP_ENV=production RESEND_API_KEY=""
export DATABASE_URL="postgresql+asyncpg://e2e:e2e@127.0.0.1:55434/e2e"
export TURNSTILE_SITE_KEY="0x4AAAAAAE2tR3JA0hccEw-z"
export TURNSTILE_SECRET_KEY="0x4AAAAAAE2tR59cwgNA9i-cvaI-kZ6ttJE"

.venv/bin/python - <<'EOF'
import asyncio
from app.core.database import Base, engine
async def mk():
    async with engine.begin() as c: await c.run_sync(Base.metadata.create_all)
    await engine.dispose()
asyncio.run(mk())
print("schema ready")
EOF

.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8099 >/tmp/cp_live.log 2>&1 &
SRV=$!
for i in $(seq 1 30); do curl -sf http://127.0.0.1:8099/ -o /dev/null && break; sleep 1; done

echo "=== favicon routes ==="
for p in /favicon.ico /static/favicon-16x16.png /static/favicon-32x32.png \
         /static/apple-touch-icon.png /static/icon-192.png /static/site.webmanifest; do
  printf "%-32s %s %s\n" "$p" \
    "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8099$p)" \
    "$(curl -s -o /dev/null -w '%{content_type}' http://127.0.0.1:8099$p)"
done

echo "=== favicon referenced in pages ==="
for p in / /dashboard/login /dashboard/register; do
  printf "%-22s icon-links=%s\n" "$p" "$(curl -s http://127.0.0.1:8099$p | grep -c 'rel="icon"')"
done

echo "=== turnstile widget rendered with REAL sitekey ==="
for p in /dashboard/register /dashboard/forgot-password; do
  printf "%-28s widget=%s script=%s\n" "$p" \
   "$(curl -s http://127.0.0.1:8099$p | grep -c 'cf-turnstile')" \
   "$(curl -s http://127.0.0.1:8099$p | grep -c 'challenges.cloudflare.com')"
done

echo "=== signup WITHOUT solving challenge (the attack) ==="
curl -s -o /dev/null -w 'JSON  /auth/register        -> %{http_code}\n' \
  -X POST http://127.0.0.1:8099/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"bot@corporate-victim.com","password":"hunter22"}'
curl -s -o /dev/null -w 'FORM  /dashboard/register   -> %{http_code}\n' \
  -X POST http://127.0.0.1:8099/dashboard/register \
  -d 'email=bot2@corporate-victim.com&password=hunter22&confirm_password=hunter22'
curl -s -o /dev/null -w 'FORM  /forgot-password      -> %{http_code}\n' \
  -X POST http://127.0.0.1:8099/dashboard/forgot-password -d 'email=bot@x.com'
curl -s -o /dev/null -w 'FORM  /resend-verification  -> %{http_code}\n' \
  -X POST http://127.0.0.1:8099/dashboard/resend-verification -d 'email=bot@x.com'

echo "=== users actually created ==="
docker exec cp-live-pg psql -U e2e -d e2e -At -c 'select count(*) from users'

kill $SRV 2>/dev/null
docker rm -f cp-live-pg >/dev/null 2>&1
