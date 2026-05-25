# Pre-launch verification

A consolidated, runnable checklist of the items that need real credentials, real inboxes, or real provider endpoints to verify. Everything in this file should be ticked off before billing real customers.

Items already verified in dev (no action needed):

- ✅ Multi-region consensus: us / eu / asia workers heartbeating, monitor flipped DOWN on `httpbin.org/status/500` once all 3 regions agreed
- ✅ Fresh signup → first incident E2E (alert delivery row created, retried, marked failed against bogus URL — pipeline works end to end)
- ✅ Multi-tenant isolation: 5 cross-account URLs all returned 404
- ✅ Health endpoints: `/health` and `/healthz/beat` both 200
- ✅ Stripe webhook signature rejection: `/billing/webhook` with bad `stripe-signature` → 400
- ✅ Worker secret rejection: `/internal/*` with bad `X-Worker-Secret` → 403
- ✅ CSRF posture: JWT cookies set `SameSite=Lax`, `secure` in prod
- ✅ Mobile responsiveness: landing/docs/dashboard fit a 390px viewport, plans table scrolls inside its container
- ✅ Account-delete endpoint at `POST /dashboard/account/delete` (cascades through projects → monitors → checks)

---

## 1. Stripe end-to-end (test mode)

You need your own card to walk the full upgrade/downgrade/cancel loop. The webhook signature rejection is already verified, so what's left is the happy paths.

1. From a fresh account, `/dashboard/billing` → Start Starter. Use card `4242 4242 4242 4242`, any future expiry, any CVC.
2. Confirm `checkout.session.completed` arrives in the Stripe dashboard's webhook log, returns 200.
3. Confirm `user.plan` flips to `STARTER` and `user.subscription_status='active'` via:
   ```bash
   docker compose exec db psql -U uptimebot -c "SELECT email, plan, subscription_status, current_period_end FROM users WHERE email='YOUR_TEST_EMAIL';"
   ```
4. Repeat for Pro upgrade.
5. Cancel the subscription via the Stripe customer portal. Confirm `subscription_status='canceled'` and `current_period_end` is set in the future.
6. **Critical**: until `current_period_end`, the user should still be on the paid plan. Verify by trying to add a Starter-allowed monitor count.
7. Walk `/dashboard/account/delete` with an active subscription — confirm it refuses with "Cancel your subscription on the Billing page before deleting".

If using **live mode** for the first time, the Stripe Webhooks dashboard's "Send test event" feature with your live signing secret is the right last-mile check.

## 2. Email deliverability

The Resend code path is verified; what's not verified is whether your sending domain authenticates correctly.

**DNS records to add on the sending domain** (`checkpulse.dev` or whatever you're sending from):

- **SPF**: TXT record at the root: `v=spf1 include:resend.dev ~all` (or `-all` once stable)
- **DKIM**: Resend gives you 1–3 CNAME records (named like `resend._domainkey.checkpulse.dev`) — copy them from the Resend domain settings page exactly.
- **DMARC**: TXT at `_dmarc.checkpulse.dev`: start with `v=DMARC1; p=none; rua=mailto:postmaster@checkpulse.dev` so you get reports without bouncing. Tighten to `p=quarantine` then `p=reject` once SPF+DKIM are clean.

**Verification:**
```bash
dig +short TXT checkpulse.dev | grep spf1
dig +short CNAME resend._domainkey.checkpulse.dev
dig +short TXT _dmarc.checkpulse.dev
```

**Inbox tests**: register a fresh account using each of: a real Gmail address, a real Outlook/Hotmail, a real ProtonMail. Confirm the verification email lands in the inbox (not spam) for all three. Repeat for the password-reset email.

A practical receiver-side check is to send a test message to `check-auth@verifier.port25.com` (replies with a full SPF/DKIM/DMARC report) or use `mail-tester.com` (paste their address into the "To" of a test send, then open their link for a score out of 10).

## 3. Real-provider alert delivery

Each channel type is wired and the `_monitor_target()` helper renders the right per-type identifier. What's not verified is that each provider accepts the actual payload.

For every channel type, the fastest sanity check is `Send test` on the alert channel page (it calls `send_test_alert_sync`). But that only proves the channel is configured. To prove real incident payloads work, drive a real DOWN event with a real channel.

| Channel | Setup | Test |
|---|---|---|
| Slack | Create an Incoming Webhook for a test channel (`#checkpulse-test`). Paste URL. | Hit Send test → real message in channel. Then create monitor against `httpbin.org/status/500`, wait ~90s, confirm real DOWN embed lands. |
| Discord | Server Settings → Integrations → Webhooks → New Webhook. Paste URL. | Same as Slack. |
| Telegram | `@BotFather` → `/newbot`, copy bot token. Send a message to your bot, then `https://api.telegram.org/bot<TOKEN>/getUpdates` to get your chat ID. | Same as Slack. |
| Email | SMTP host/user/pass + from/to addresses. Try Gmail SMTP with an app password if you don't have provider creds yet. | Same as Slack, plus confirm the email lands in the configured to_email. |
| Webhook | Use a real `webhook.site/<your-id>` URL for one-shot inspection or your own receiver. | Same as Slack, plus inspect the JSON payload includes `monitor.type` and `monitor.target` as well as `monitor.url`. |

**Bulk verify**: configure all five against a single test project, then trigger one DOWN. All five should fire in parallel.

## 4. Remaining known gaps

- **DB backups for the production host** — outside this codebase. Confirm `pg_dump` runs nightly to off-host storage (S3/object storage) and that a restore drill has been done on a separate machine.
- **Privacy policy and ToS** — `/legal/privacy` and `/legal/terms` render with placeholder text marked `[REPLACE WITH ...]`. Replace before launch. A lawyer review is recommended; at minimum, copy from a reputable SaaS template and adapt.
- **Asia worker on prod** — confirmed working in dev with all 3 regions. Make sure all 3 region containers actually start on prod (check `/internal/workers` against the live host).
- **Load testing** — none performed. A single `hey -n 5000 -c 50 https://yourapp/health` on the production host will catch obvious capacity issues. Real load against the worker → API path (e.g. simulate 200 concurrent monitors at 30s interval) is the next step if you expect more than a few dozen customers at launch.
- **Rate limiting** — slowapi is wired with a 60/minute default. Confirm by hammering `/auth/login` from a single IP and watching for 429.

Tick each box on this list before going live.
