# CheckPulse — Missing & Incomplete Features

Tracked against BUILD_PLAN.md, MARKET.md, and the current codebase.
Last updated: 2026-03-25


## Week 7 (Dashboard + Polish)

- [x] **Edit monitor form in dashboard** — Modal on monitor detail page, POST handler at `/dashboard/monitors/{id}/edit`.
- [x] **Worker health monitoring / heartbeat** — `WorkerHeartbeat` model, `POST /internal/heartbeat` endpoint, `GET /internal/workers` status endpoint. Worker script sends heartbeats every ~60s.


## Week 8 (Launch Prep)

- [x] **Landing page** — `GET /` with MARKET.md copy: hero, problem, features, how-it-works, pricing, comparison, FAQ, CTA.
- [x] **Plan limit enforcement** — `app/services/plans.py` enforces project/monitor/interval limits per plan on both REST API and dashboard.
- [x] **Rate limiting** — `slowapi` with 60 req/min default, stricter on auth endpoints.
- [x] **Check retention cleanup** — Celery task deletes checks older than 90 days, runs daily at 3am UTC.
- [x] **Getting-started docs** — `GET /docs/getting-started` with setup guide.
- [x] **Stripe integration** — `POST /billing/checkout` creates Stripe checkout sessions, `POST /billing/webhook` handles payment events and plan upgrades. Needs `STRIPE_*` env vars configured.
- [ ] **Dogfooding** — Test the app end-to-end with a real bot/project to shake out bugs.
- [ ] **Soft launch** — Post in Discord bot dev communities, r/discord_bots, r/selfhosted, Twitter/X.


## Post-MVP Backlog

- [x] **Slack webhook integration** — Added `SLACK` alert type with rich Slack attachments.
- [x] **Webhook notifications (generic POST)** — Added `WEBHOOK` alert type, sends JSON payload to arbitrary URL with optional auth header.
- [x] **API key auth** — `ApiKey` model, `POST/GET/DELETE /api-keys` endpoints. `X-API-Key` header accepted as alternative to JWT on all authenticated endpoints.
- [ ] SSL certificate expiry monitoring
- [ ] Cron job / heartbeat monitoring ("dead man's switch")
- [ ] Custom domains for status pages (StatusPage model needs a `custom_domain` field)
- [ ] Team members / multi-user projects
- [ ] Response body keyword matching (check response body for expected/unexpected strings)
- [ ] Maintenance windows (suppress alerts during scheduled maintenance)


## From MARKET.md

- [ ] **Status page embed widget for Discord** — Generate an embeddable snippet or image for Discord server info channels.
- [ ] **Custom status page domain** — Allow Pro users to point their own domain at their status page.


## Database Migration Required

Run `alembic upgrade head` to apply migration `a1b2c3d4e5f6` which adds:
- `worker_heartbeats` table
- `api_keys` table
- `slack` and `webhook` values to `alerttype` enum
