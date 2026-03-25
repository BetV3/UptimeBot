# UptimeBot

Uptime monitoring and status pages for Discord/Telegram bot developers.

Multi-region HTTP monitoring with incident detection, alerting (Discord, Telegram, Email), public status pages, and a web dashboard.

## Architecture

```
[Dashboard UI] --> [FastAPI API] --> [PostgreSQL]
                        |
                  [Redis + Celery]
                        |
        +---------------+---------------+
        |               |               |
   [Worker US]    [Worker EU]     [Worker Asia]
```

- **Core** (homelab/server): API, database, Redis, Celery Beat
- **Workers** (VPS): poll `/internal/jobs`, run HTTP checks, POST results back
- **Consensus**: a monitor is DOWN only when 2+ of 3 regions report failure

## Stack

- **API**: FastAPI + SQLAlchemy (async) + PostgreSQL
- **Task queue**: Celery + Redis
- **Workers**: standalone Python script using httpx
- **Dashboard**: Jinja2 templates + Tailwind CSS
- **Auth**: JWT (access + refresh tokens)

## Quick Start

```bash
# Clone and configure
git clone https://github.com/BetV3/UptimeBot.git
cd UptimeBot
cp .env .env  # edit secrets for production

# Start all services
docker compose up -d

# Run migrations
docker compose exec api sh -c "PYTHONPATH=/code alembic upgrade head"

# Verify
curl http://localhost:8000/health
# {"status":"healthy","checks":{"database":"ok","redis":"ok"}}
```

The dashboard is available at `http://localhost:8000/dashboard`.

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `DATABASE_URL` | Async PostgreSQL connection string | `postgresql+asyncpg://uptimebot:localdev@db:5432/uptimebot` |
| `DATABASE_URL_SYNC` | Sync PostgreSQL connection string (Celery/Alembic) | `postgresql://uptimebot:localdev@db:5432/uptimebot` |
| `REDIS_URL` | Redis connection string | `redis://redis:6379/0` |
| `SECRET_KEY` | JWT signing key | `change-me-in-production` |
| `WORKER_SECRET` | Shared secret for worker auth | `change-me-worker-secret` |
| `APP_ENV` | `development` or `production` | `development` |

## Running a Check Worker

Workers are standalone scripts that run on VPS instances. Each worker monitors from a specific region:

```bash
pip install httpx
python worker/run.py \
    --region us \
    --api-url http://your-api:8000 \
    --secret your-worker-secret \
    --poll-interval 10
```

Or via environment variables: `REGION`, `API_URL`, `WORKER_SECRET`, `POLL_INTERVAL`.

## API Endpoints

### Auth
- `POST /auth/register` — create account, returns tokens
- `POST /auth/login` — returns access + refresh tokens
- `POST /auth/refresh` — refresh access token
- `GET /auth/me` — current user

### Projects
- `POST /projects` — create project
- `GET /projects` — list projects
- `GET /projects/{id}` — project detail
- `PATCH /projects/{id}` — update project
- `DELETE /projects/{id}` — delete project

### Monitors
- `POST /projects/{id}/monitors` — create monitor
- `GET /projects/{id}/monitors` — list monitors
- `GET /monitors/{id}` — monitor detail
- `PATCH /monitors/{id}` — update monitor
- `DELETE /monitors/{id}` — delete monitor
- `POST /monitors/{id}/pause` — pause monitoring
- `POST /monitors/{id}/resume` — resume monitoring
- `GET /monitors/{id}/checks` — paginated check history
- `GET /monitors/{id}/checks/summary` — uptime % (24h/7d/30d/90d)

### Incidents
- `GET /projects/{id}/incidents` — list incidents
- `GET /incidents/{id}` — incident detail with check timeline

### Alert Channels
- `POST /projects/{id}/alerts` — create alert channel
- `GET /projects/{id}/alerts` — list channels
- `PATCH /alerts/{id}` — update channel
- `DELETE /alerts/{id}` — delete channel
- `POST /alerts/{id}/test` — send test notification

### Status Page
- `GET /projects/{id}/status-page` — get settings
- `PATCH /projects/{id}/status-page` — update settings
- `GET /status/{slug}` — public status page (no auth)

### Internal (worker endpoints)
- `GET /internal/jobs?region=us` — get pending checks (requires `X-Worker-Secret`)
- `POST /internal/results` — submit check results (requires `X-Worker-Secret`)

### Health
- `GET /health` — service health check

## Project Structure

```
app/
  api/routes/       # API + dashboard route handlers
  core/             # config, database
  models/           # SQLAlchemy models
  schemas/          # Pydantic schemas
  services/         # auth, alerts
  templates/        # Jinja2 dashboard templates
  workers/          # Celery app + tasks
worker/             # Standalone VPS worker script
alembic/            # Database migrations
tests/              # Pytest tests
```

## Development

```bash
# Run tests (with services running)
docker compose exec api sh -c "PYTHONPATH=/code pytest tests/ -v"

# Generate a new migration after model changes
docker compose exec api sh -c "PYTHONPATH=/code alembic revision --autogenerate -m 'description'"

# Apply migrations
docker compose exec api sh -c "PYTHONPATH=/code alembic upgrade head"

# API docs
open http://localhost:8000/docs
```

## License

MIT
