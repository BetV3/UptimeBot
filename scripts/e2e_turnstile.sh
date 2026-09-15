#!/usr/bin/env bash
# End-to-end proof against a REAL Postgres + REAL Cloudflare siteverify.
# Cloudflare's documented Turnstile testing secrets:
#   1x0000000000000000000000000000000AA = always passes
#   2x0000000000000000000000000000000AA = always fails
set -u
cd /home/bet/checkpulse-ai/repo
PY=.venv/bin/python

docker rm -f cp-e2e-pg >/dev/null 2>&1
docker run -d --name cp-e2e-pg -e POSTGRES_PASSWORD=e2e -e POSTGRES_USER=e2e -e POSTGRES_DB=e2e \
  -p 55433:5432 postgres:16-alpine >/dev/null
for i in $(seq 1 40); do docker exec cp-e2e-pg pg_isready -U e2e >/dev/null 2>&1 && break; sleep 1; done

export APP_ENV=production
export RESEND_API_KEY=""
export DATABASE_URL="postgresql+asyncpg://e2e:e2e@127.0.0.1:55433/e2e"
export TURNSTILE_SITE_KEY="1x00000000000000000000AA"

run_case () {
  docker exec cp-e2e-pg psql -U e2e -d e2e -c 'drop schema public cascade; create schema public;' >/dev/null 2>&1
  TURNSTILE_SECRET_KEY="$1" $PY - <<'EOF'
import asyncio, json
from app.core.config import get_settings
get_settings.cache_clear()
import httpx
from app.main import app
from app.core.database import Base, engine

async def main():
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    tr = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=tr, base_url="http://test") as c:
        # Scripted client that solves nothing -> no token field at all.
        r0 = await c.post("/auth/register",
                          json={"email":"noprobe@example.com","password":"hunter22"})
        # Client presenting a token -> real Cloudflare siteverify decides.
        r1 = await c.post("/auth/register",
                          json={"email":"probe@example.com","password":"hunter22",
                                "cf-turnstile-response":"XXXX.DUMMY.TOKEN.XXXX"})
    from sqlalchemy import text
    async with engine.connect() as c2:
        n = (await c2.execute(text("select count(*) from users"))).scalar()
    await engine.dispose()
    print(json.dumps({"no_token": r0.status_code,
                      "with_token": r1.status_code,
                      "users_created": n}))

asyncio.run(main())
EOF
}

echo "### CASE 1: secret = ALWAYS-FAIL  (expect 403/403, 0 users)"
run_case "2x0000000000000000000000000000000AA"
echo
echo "### CASE 2: secret = ALWAYS-PASS  (expect 403 no-token / 202 with-token, 1 user)"
run_case "1x0000000000000000000000000000000AA"
echo
echo "### CASE 3: gate DISABLED (empty secret) (expect 202/202, 2 users)"
run_case ""

docker rm -f cp-e2e-pg >/dev/null 2>&1
