# UptimeBot - Build Plan

Uptime monitoring and status pages for Discord/Telegram bot developers.

**Stack:** FastAPI, PostgreSQL, Redis, Celery, httpx
**Target:** Discord/Telegram bot devs who monetize their bots
**MVP timeline:** 6-8 weeks at 20-40 hrs/week


## Architecture

```
[Dashboard UI] --> [FastAPI API] --> [PostgreSQL]
                        |
                  [Redis + Celery]
                        |
        +---------------+---------------+
        |               |               |
   [Worker US]    [Worker EU]     [Worker Asia]
   (VPS ~$5)      (VPS ~$4)      (VPS ~$5)
```

- Homelab hosts: API, database, Redis, Celery Beat
- VPS workers poll `/internal/jobs`, run HTTP checks, POST results back
- "Down" = failed from 2+ of 3 regions (avoids false positives)


## Week 1: Foundation [CURRENT]

**Goal:** `GET /health` returns 200, database connected, models migrated.

- [ ] Initialize git repo, push to GitHub
- [ ] Review project structure (already scaffolded)
- [ ] `docker-compose up` -- verify PostgreSQL + Redis start cleanly
- [ ] Run `alembic revision --autogenerate -m "initial"` to generate first migration
- [ ] Run `alembic upgrade head` to apply migration
- [ ] Verify `GET /health` returns healthy status with both checks passing
- [ ] Add indexes on `checks.checked_at` and `checks.monitor_id` (will matter at scale)
- [ ] Write a quick smoke test: `curl localhost:8000/health`
- [ ] Commit and push

**Done when:** `docker-compose up` -> `curl localhost:8000/health` -> `{"status": "healthy"}`


## Week 2: Auth + Projects

**Goal:** Register, login, create/list projects via API.

- [ ] **Pydantic schemas:** UserCreate, UserResponse, Token, ProjectCreate, ProjectResponse
- [ ] **Auth service:** password hashing (passlib/bcrypt), JWT creation/validation
- [ ] **Auth endpoints:**
  - [ ] `POST /auth/register` -- create user, return token
  - [ ] `POST /auth/login` -- validate credentials, return access + refresh tokens
  - [ ] `POST /auth/refresh` -- refresh access token
  - [ ] `GET /auth/me` -- return current user
- [ ] **Auth dependency:** `get_current_user` FastAPI dependency using JWT
- [ ] **Project endpoints:**
  - [ ] `POST /projects` -- create project (auto-generate slug from name)
  - [ ] `GET /projects` -- list user's projects
  - [ ] `GET /projects/{id}` -- get project detail
  - [ ] `PATCH /projects/{id}` -- update project
  - [ ] `DELETE /projects/{id}` -- delete project + cascade
- [ ] **Ownership guard:** users can only access their own projects
- [ ] Test all endpoints with curl or httpie

**Done when:** Can register, login, CRUD projects, and non-owners get 403.


## Week 3: Monitors + Scheduling

**Goal:** Monitors created, pending checks appear on schedule via Celery Beat.

- [ ] **Monitor schemas:** MonitorCreate, MonitorResponse, MonitorUpdate
- [ ] **Monitor endpoints:**
  - [ ] `POST /projects/{id}/monitors` -- create monitor
  - [ ] `GET /projects/{id}/monitors` -- list monitors for project
  - [ ] `GET /monitors/{id}` -- get monitor detail
  - [ ] `PATCH /monitors/{id}` -- update monitor
  - [ ] `DELETE /monitors/{id}` -- delete monitor
  - [ ] `POST /monitors/{id}/pause` -- set is_active=false
  - [ ] `POST /monitors/{id}/resume` -- set is_active=true
- [ ] **PendingCheck model** (or use Redis sorted set):
  - monitor_id, region, scheduled_at
- [ ] **Celery Beat task:** `schedule_pending_checks`
  - Runs every 15 seconds
  - Finds monitors where `now - last_check >= interval_seconds`
  - Creates pending checks for each region (us, eu, asia)
- [ ] **Internal endpoint:** `GET /internal/jobs?region=us`
  - Requires `X-Worker-Secret` header
  - Returns batch of pending checks for that region
  - Marks them as claimed (prevent double-processing)
- [ ] Uncomment the beat_schedule in celery_app.py

**Done when:** Create a monitor, wait, see pending checks accumulating.


## Week 4: Check Workers

**Goal:** End-to-end: monitor created -> checks run -> results saved.

- [ ] **Worker script** (standalone Python, runs on VPS):
  - Polls `GET /internal/jobs?region={region}` every 10 seconds
  - For each job: makes HTTP request to target URL
  - Records: status_code, response_time_ms, error (if any)
  - POSTs results to `POST /internal/results`
- [ ] **Internal results endpoint:** `POST /internal/results`
  - Accepts batch of check results
  - Saves to checks table
  - Updates monitor.current_status based on consensus logic:
    - 2+ regions down = monitor is DOWN
    - Otherwise = UP
- [ ] **Check history endpoints:**
  - [ ] `GET /monitors/{id}/checks` -- paginated check history
  - [ ] `GET /monitors/{id}/checks/summary` -- uptime % for last 24h/7d/30d/90d
- [ ] Deploy worker to ONE VPS to validate the flow
- [ ] Monitor the checks table -- verify results coming in

**Done when:** Monitor a real URL, see check results with response times from at least 1 region.


## Week 5: Incidents + Alerting

**Goal:** Monitor goes down -> incident created -> Discord/Telegram notified.

- [ ] **Incident detection** (in the results processing logic):
  - When monitor status transitions UP -> DOWN: create Incident
  - When monitor status transitions DOWN -> UP: resolve Incident (set resolved_at)
- [ ] **Incident endpoints:**
  - [ ] `GET /projects/{id}/incidents` -- list incidents
  - [ ] `GET /incidents/{id}` -- incident detail with timeline
- [ ] **AlertChannel CRUD:**
  - [ ] `POST /projects/{id}/alerts` -- create channel
  - [ ] `GET /projects/{id}/alerts` -- list channels
  - [ ] `PATCH /alerts/{id}` -- update channel
  - [ ] `DELETE /alerts/{id}` -- delete channel
  - [ ] `POST /alerts/{id}/test` -- send test notification
- [ ] **Alert integrations:**
  - [ ] Discord webhook (POST to webhook URL with embed)
  - [ ] Telegram bot (send message via Bot API)
  - [ ] Email (SMTP, can use free tier of Resend/Mailgun)
- [ ] **Alert dispatch:** when incident created/resolved, fire alerts to all active channels

**Done when:** Intentionally break a monitored URL, get a Discord notification within 2 minutes.


## Week 6: Status Page

**Goal:** Public status page viewable at `yourapp.com/status/my-bot`.

- [ ] **StatusPage endpoints:**
  - [ ] `GET /projects/{id}/status-page` -- get settings
  - [ ] `PATCH /projects/{id}/status-page` -- update settings
- [ ] **Public endpoint (no auth):** `GET /status/{slug}`
  - Returns: display_name, logo, color, overall_status, monitors with uptime %, recent incidents
- [ ] **Uptime calculation:** 90-day uptime percentage per monitor
- [ ] **Status page frontend:**
  - Server-rendered HTML or minimal React page
  - Shows current status per monitor (green/red dot)
  - 90-day uptime bar (like GitHub status)
  - Recent incidents list
  - Customizable: name, logo, accent color
- [ ] Auto-create StatusPage when Project is created (sensible defaults)

**Done when:** Visit a public URL in browser, see a clean status page with live data.


## Week 7: Dashboard + Polish

**Goal:** Fully usable via web UI, all 3 regions active.

- [ ] **Frontend dashboard** (React + Tailwind or server-rendered templates):
  - [ ] Login / Register pages
  - [ ] Project list
  - [ ] Monitor list with status indicators
  - [ ] Add/edit monitor form
  - [ ] Alert channel management
  - [ ] Simple uptime charts (response time over time)
  - [ ] Incident history view
- [ ] Deploy remaining 2 VPS workers (EU, Asia)
- [ ] Worker health monitoring (heartbeat check -- know if a worker goes silent)
- [ ] Fix any bugs from end-to-end testing

**Done when:** Non-technical user could sign up and set up monitoring through the UI.


## Week 8: Launch Prep

**Goal:** MVP live, first users.

- [ ] **Landing page:** what it does, pricing, sign up CTA
- [ ] **Pricing implementation:**
  - Free: 3 monitors, 5-min intervals, 1 project
  - Starter ($5/mo): 10 monitors, 1-min intervals, 3 projects
  - Pro ($15/mo): 50 monitors, 30-sec intervals, unlimited projects, custom domain
- [ ] Stripe integration (or start free-only, add payments week 9)
- [ ] Rate limiting on API endpoints
- [ ] Basic docs / getting started guide
- [ ] Check retention cleanup job (delete raw checks older than 90 days)
- [ ] Test with your own bot/project as dogfooding
- [ ] **Soft launch:**
  - Post in 2-3 Discord bot developer communities
  - Post on r/discord_bots, r/selfhosted
  - Share on Twitter/X

**Done when:** Real users signing up and monitoring their bots.


## Post-MVP Features (backlog)

- SSL certificate expiry monitoring
- Cron job / heartbeat monitoring ("dead man's switch")
- Custom domains for status pages
- Team members / multi-user projects
- Slack webhook integration
- Response body keyword matching
- Maintenance windows
- API key auth (alternative to JWT for programmatic access)
- Webhook notifications (generic POST)


## Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-01-18 | FastAPI + PostgreSQL + Redis | Matches existing skills, async-first |
| 2026-01-18 | 3-region VPS workers | Credible multi-region without cloud costs |
| 2026-01-18 | Discord/Telegram bot devs | Clear pain point, accessible community |
| 2026-01-18 | Homelab for core, VPS for workers | $0 core hosting, ~$15/mo for workers |
| 2026-03-24 | Restarting fresh with same plan | Plan is solid, execution is what matters |
