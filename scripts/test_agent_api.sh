#!/usr/bin/env bash
# Throwaway Postgres + Redis for the agent-API tests, then migrate and run pytest.
#   scripts/test_agent_api.sh            (cleans up the containers at the end)
set -euo pipefail
cd "$(dirname "$0")/.."
PGP=55432; RDP=56379
docker rm -f cp-test-pg cp-test-redis >/dev/null 2>&1 || true
docker run -d --name cp-test-pg -e POSTGRES_USER=uptimebot -e POSTGRES_PASSWORD=testpw \
  -e POSTGRES_DB=uptimebot -p 127.0.0.1:$PGP:5432 postgres:16-alpine >/dev/null
docker run -d --name cp-test-redis -p 127.0.0.1:$RDP:6379 redis:7-alpine >/dev/null
trap 'docker rm -f cp-test-pg cp-test-redis >/dev/null 2>&1 || true' EXIT
for i in $(seq 1 40); do
  docker exec cp-test-pg pg_isready -U uptimebot >/dev/null 2>&1 && break; sleep 1
done
sleep 2
export DATABASE_URL="postgresql+asyncpg://uptimebot:testpw@127.0.0.1:$PGP/uptimebot"
export DATABASE_URL_SYNC="postgresql://uptimebot:testpw@127.0.0.1:$PGP/uptimebot"
export REDIS_URL="redis://127.0.0.1:$RDP/0"
export APP_ENV=test CP_AGENT_API_TEST=1
.venv/bin/alembic upgrade head 2>&1 | tail -2
.venv/bin/python -m pytest -q tests/test_agent_api.py "$@"
