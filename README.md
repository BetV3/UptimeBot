# UptimeBot

Uptime monitoring and status pages for Discord/Telegram bot developers.

Multi-region HTTP monitoring with incident detection, alerting (Discord, Telegram, Email, Slack, Webhooks), public status pages, and a web dashboard.

## Architecture

```
                        ┌──────────────┐
                        │  PostgreSQL   │
                        └──────┬───────┘
                               │
┌──────────┐     ┌─────────────┴─────────────┐     ┌─────────┐
│ Dashboard │────▶│     FastAPI API Server     │◀────│  Redis  │
│  (Web UI) │     └─────────────┬─────────────┘     └────┬────┘
└──────────┘          ▲    ▲    ▲                        │
                      │    │    │                  ┌─────┴─────┐
               ┌──────┘    │    └──────┐          │   Celery   │
               │           │           │          │  Beat/Work │
          ┌────┴────┐ ┌────┴────┐ ┌────┴────┐    └───────────┘
          │Worker US│ │Worker EU│ │Worker AP│
          └─────────┘ └─────────┘ └─────────┘
```

- **Main server**: FastAPI API, PostgreSQL, Redis, Celery (Beat + Worker)
- **Check workers**: Standalone Python scripts on remote VPS nodes. Poll the API for jobs, run HTTP checks, report results back.
- **Consensus**: A monitor is marked DOWN only when 2+ regions report failure.

## Stack

| Layer | Technology |
|---|---|
| API | FastAPI + SQLAlchemy (async) + PostgreSQL |
| Task queue | Celery + Redis |
| Check workers | Standalone Python script (`httpx`) |
| Dashboard | Jinja2 templates + Tailwind CSS |
| Auth | JWT (access + refresh tokens) + API key support |
| Payments | Stripe Checkout + Webhooks |
| Rate limiting | slowapi (60 req/min default) |

---

## Prerequisites

- **Docker** and **Docker Compose** (for the main server)
- **Python 3.12+** (for running check workers on remote nodes)

---

## Running the Main Server

The main server includes the FastAPI app, PostgreSQL, Redis, and Celery. Everything runs via Docker Compose.

### 1. Clone and configure

```bash
git clone https://github.com/BetV3/UptimeBot.git
cd UptimeBot
```

### 2. Set up environment variables

Copy and edit the `.env` file:

```bash
cp .env .env.local  # make a copy, then edit
```

At minimum, change these for production:

```env
# REQUIRED — change these from defaults
SECRET_KEY=your-random-secret-here          # JWT signing key (use: openssl rand -hex 32)
WORKER_SECRET=your-worker-secret-here       # Shared secret between API and check workers

# Database (defaults work with Docker Compose)
DATABASE_URL=postgresql+asyncpg://uptimebot:localdev@db:5432/uptimebot
DATABASE_URL_SYNC=postgresql://uptimebot:localdev@db:5432/uptimebot

# Redis (defaults work with Docker Compose)
REDIS_URL=redis://redis:6379/0

# App
APP_NAME=UptimeBot
APP_ENV=development                         # Set to "production" for prod
APP_URL=http://localhost:8000               # Your public URL (used for Stripe redirects)

# Stripe (optional — billing won't work without these)
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=
STRIPE_STARTER_PRICE_ID=
STRIPE_PRO_PRICE_ID=
```

### 3. Start all services

```bash
docker compose up -d
```

This starts 5 containers:

| Container | Service | Purpose |
|---|---|---|
| `uptimebot-db-1` | PostgreSQL 16 | Database |
| `uptimebot-redis-1` | Redis 7 | Task queue broker + cache |
| `uptimebot-api-1` | FastAPI (uvicorn) | API server on port 8000 |
| `uptimebot-celery-worker-1` | Celery worker | Processes background tasks (alerts, cleanup) |
| `uptimebot-celery-beat-1` | Celery beat | Schedules checks every 15s, cleanup daily at 3am UTC |

### 4. Run database migrations

```bash
docker compose exec api sh -c "PYTHONPATH=/code alembic upgrade head"
```

### 5. Verify everything is running

```bash
# Check container status
docker compose ps

# Health check (tests database + Redis connectivity)
curl http://localhost:8000/health
# → {"status":"healthy","checks":{"database":"ok","redis":"ok"}}
```

### 6. Access the app

| URL | What |
|---|---|
| `http://localhost:8000` | Landing page |
| `http://localhost:8000/dashboard` | Dashboard (login required) |
| `http://localhost:8000/dashboard/register` | Create an account |
| `http://localhost:8000/api/docs` | Interactive API docs (Swagger) |
| `http://localhost:8000/api/redoc` | API docs (ReDoc) |
| `http://localhost:8000/docs/getting-started` | Getting started guide |

### Stopping / restarting

```bash
docker compose down       # Stop all containers (data persists in volumes)
docker compose down -v    # Stop and delete all data (including database)
docker compose restart    # Restart all containers
docker compose logs -f    # Tail logs from all containers
docker compose logs api   # Logs from just the API
```

---

## Running Check Workers

Check workers are **standalone scripts** that run on separate machines (VPS nodes in different regions). They don't need Docker or access to the database — they only communicate with the API server over HTTP.

### On each worker node:

```bash
# 1. Copy the worker directory to the VPS
scp -r worker/ user@vps:/opt/uptimebot-worker/

# 2. SSH in and install the single dependency
ssh user@vps
cd /opt/uptimebot-worker
pip install httpx

# 3. Run the worker
python run.py \
    --region us \
    --api-url https://your-server.com \
    --secret your-worker-secret \
    --poll-interval 10
```

### Worker CLI options

| Flag | Env var | Default | Description |
|---|---|---|---|
| `--region` | `REGION` | `us` | Region identifier (`us`, `eu`, `asia`) |
| `--api-url` | `API_URL` | `http://localhost:8000` | URL of the main API server |
| `--secret` | `WORKER_SECRET` | `change-me-worker-secret` | Must match `WORKER_SECRET` on the server |
| `--poll-interval` | `POLL_INTERVAL` | `10` | Seconds between polling for jobs |

### Running as a systemd service (recommended for production)

Create `/etc/systemd/system/uptimebot-worker.service`:

```ini
[Unit]
Description=UptimeBot Check Worker
After=network.target

[Service]
Type=simple
User=uptimebot
WorkingDirectory=/opt/uptimebot-worker
Environment=REGION=us
Environment=API_URL=https://your-server.com
Environment=WORKER_SECRET=your-worker-secret
ExecStart=/usr/bin/python3 run.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now uptimebot-worker
sudo journalctl -u uptimebot-worker -f   # view logs
```

### Multi-region setup

Deploy one worker per region for consensus-based monitoring:

```
VPS (US)    → python run.py --region us    --api-url https://your-server.com --secret ...
VPS (EU)    → python run.py --region eu    --api-url https://your-server.com --secret ...
VPS (Asia)  → python run.py --region asia  --api-url https://your-server.com --secret ...
```

The API uses consensus logic: a monitor is only marked DOWN when **2 or more regions** report failure. This prevents false alerts from single-region network issues.

---

## Environment Variables Reference

| Variable | Description | Default |
|---|---|---|
| `DATABASE_URL` | Async PostgreSQL connection string | `postgresql+asyncpg://uptimebot:localdev@db:5432/uptimebot` |
| `DATABASE_URL_SYNC` | Sync PostgreSQL (Celery/Alembic) | `postgresql://uptimebot:localdev@db:5432/uptimebot` |
| `REDIS_URL` | Redis connection string | `redis://redis:6379/0` |
| `SECRET_KEY` | JWT signing key — **change in production** | `change-me-in-production` |
| `WORKER_SECRET` | Shared secret for worker auth — **change in production** | `change-me-worker-secret` |
| `APP_ENV` | `development` or `production` | `development` |
| `APP_URL` | Public URL of the app | `http://localhost:8000` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | JWT access token lifetime | `30` |
| `REFRESH_TOKEN_EXPIRE_DAYS` | JWT refresh token lifetime | `7` |
| `STRIPE_SECRET_KEY` | Stripe API secret key | _(empty — billing disabled)_ |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signing secret | _(empty)_ |
| `STRIPE_STARTER_PRICE_ID` | Stripe Price ID for Starter plan | _(empty)_ |
| `STRIPE_PRO_PRICE_ID` | Stripe Price ID for Pro plan | _(empty)_ |

---

## API Endpoints

Full interactive docs at `/api/docs` when the server is running.

### Auth
- `POST /auth/register` — Create account, returns tokens
- `POST /auth/login` — Login, returns access + refresh tokens
- `POST /auth/refresh` — Refresh access token
- `GET /auth/me` — Current user info

### Projects
- `POST /projects` — Create project
- `GET /projects` — List projects
- `GET /projects/{id}` — Project detail
- `PATCH /projects/{id}` — Update project
- `DELETE /projects/{id}` — Delete project

### Monitors
- `POST /projects/{id}/monitors` — Create monitor
- `GET /projects/{id}/monitors` — List monitors
- `GET /monitors/{id}` — Monitor detail
- `PATCH /monitors/{id}` — Update monitor
- `DELETE /monitors/{id}` — Delete monitor
- `POST /monitors/{id}/pause` — Pause monitoring
- `POST /monitors/{id}/resume` — Resume monitoring
- `GET /monitors/{id}/checks` — Paginated check history
- `GET /monitors/{id}/checks/summary` — Uptime % (24h / 7d / 30d / 90d)

### Incidents
- `GET /projects/{id}/incidents` — List incidents
- `GET /incidents/{id}` — Incident detail with check timeline

### Alert Channels
- `POST /projects/{id}/alerts` — Create alert channel (Discord, Telegram, Email, Slack, Webhook)
- `GET /projects/{id}/alerts` — List channels
- `PATCH /alerts/{id}` — Update channel
- `DELETE /alerts/{id}` — Delete channel
- `POST /alerts/{id}/test` — Send test notification

### Status Page
- `GET /projects/{id}/status-page` — Get status page settings
- `PATCH /projects/{id}/status-page` — Update settings
- `GET /status/{slug}` — Public status page (no auth required)

### API Keys
- `POST /api-keys` — Create API key (returns raw key once)
- `GET /api-keys` — List API keys (prefix only)
- `DELETE /api-keys/{id}` — Revoke API key

### Billing
- `POST /billing/checkout?plan=starter` — Create Stripe checkout session
- `POST /billing/webhook` — Stripe webhook handler

### Internal (worker endpoints)
- `GET /internal/jobs?region=us` — Get pending checks (requires `X-Worker-Secret`)
- `POST /internal/results` — Submit check results (requires `X-Worker-Secret`)
- `POST /internal/heartbeat` — Worker heartbeat (requires `X-Worker-Secret`)
- `GET /internal/workers` — List worker status (requires `X-Worker-Secret`)

### Health
- `GET /health` — Service health check (database + Redis)

---

## Development

### Run tests

```bash
docker compose exec api sh -c "PYTHONPATH=/code pytest tests/ -v"
```

### Create a new migration

```bash
docker compose exec api sh -c "PYTHONPATH=/code alembic revision --autogenerate -m 'description'"
```

### Apply migrations

```bash
docker compose exec api sh -c "PYTHONPATH=/code alembic upgrade head"
```

### Rebuild after dependency changes

```bash
docker compose build api
docker compose up -d
```

### View API docs

Open `http://localhost:8000/api/docs` in your browser.

---

## Project Structure

```
app/
  api/routes/         # API + dashboard route handlers
  core/               # Config, database engine
  models/             # SQLAlchemy models
  schemas/            # Pydantic request/response schemas
  services/           # Auth, alerts, plan enforcement
  templates/          # Jinja2 dashboard + landing page templates
  workers/            # Celery app + scheduled tasks
worker/               # Standalone VPS check worker script
alembic/              # Database migrations
tests/                # Pytest tests
```

## Plans and Limits

| | Free | Starter | Pro |
|---|---|---|---|
| Projects | 1 | 3 | Unlimited |
| Monitors | 3 | 10 | 50 |
| Min check interval | 5 min | 1 min | 30 sec |

## License

MIT
