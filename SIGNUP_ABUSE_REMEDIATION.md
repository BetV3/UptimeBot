# Signup abuse remediation — Turnstile gate

## What happened

Between 2026-04 and 2026-07 the CheckPulse signup form was driven as an
email-validation relay. Evidence from the production database:

- 1,194 users, **2** projects, **2** monitors, **0** Stripe subscriptions.
  The only two accounts with a project are the founder and one friend.
- Signups walk an alphabetically ordered corporate email list. June 1–3 is
  solid A-domains (`actionsales.com`, `admiralbeverage.com`, `aemetis.com`,
  `alignaero.com`, `airxcel.com`, …); by late July it has reached B
  (`bauercomp.com`, `bloomingdales.com`, `brownells.com`, …).
- Pacing is ~1 signup per 100 minutes, 24/7 (avg gap 6,028s, min 998s) —
  deliberately below any frequency threshold.
- 185 of these addresses are `email_verified = true`.

Each submission caused CheckPulse to send a verification email **from
`no-reply@notifications.checkpulse.dev` via Resend to a third party who never
asked for it**. That is the damage: thousands of unsolicited messages against
the transactional sending domain that also carries customer downtime alerts.

## Why rate limiting was not the fix

`/auth/register` already carried `@limiter.limit("5/minute")` for the entire
duration of the incident. A client pacing at 0.01 req/min never approaches
that bucket. Tightening the limit would inconvenience real users and do
nothing to the attacker. The defense has to be a per-submission
proof-of-humanity.

## What shipped

Cloudflare Turnstile, chosen because Cloudflare already terminates our
traffic (CF Tunnel + proxied DNS), so there is no new vendor in the request
path and `CF-Connecting-IP` is already trusted by `app/core/ratelimit.py`.

- `app/core/turnstile.py` — siteverify client. **Fails closed**: a transport
  error, non-JSON body, or missing token is a rejection, never a fallthrough
  to allow. An attacker who can induce a timeout must not get a free bypass.
- Gate applied to **all three** mail-sending endpoints:
  - `POST /auth/register` (JSON API)
  - `POST /dashboard/register` (HTML form)
  - `POST /dashboard/resend-verification`
  - `POST /dashboard/forgot-password`
- The JSON API is gated too. Gating only the HTML form would just move the
  abusive client to JSON.
- The challenge runs **before** the user lookup, so the endpoint is not an
  email-enumeration oracle for unsolved requests.
- Token is bound to the solver's IP (`remoteip`) via the shared `client_ip`
  resolver, so a token farmed on one host and replayed from another is
  rejected.
- Widget rendered in `register.html`, `forgot_password.html`,
  `check_email.html`, conditional on `turnstile_site_key` being set.

Back-compat: with `turnstile_secret_key` empty the gate is inert, matching how
`app/services/email.py` degrades to stdout without a Resend key. Dev, tests,
and local compose need no Cloudflare account.

## Verification

Unit: `tests/test_turnstile.py` — 12 tests, all pass. Covers reject-on-missing,
reject-on-invalid, reject-on-replay (`timeout-or-duplicate`), fail-closed on
network/HTTP/non-JSON error, `remoteip` binding incl. `CF-Connecting-IP`, and
schema field aliasing.

Full suite: **49 passed**. `tests/test_health.py::test_health_returns_200`
fails identically on clean `main` (needs a live DB) — pre-existing.

End-to-end (`e2e_turnstile.sh`, real Postgres container + **real Cloudflare
siteverify** using CF's documented testing secrets):

| Secret | no token | with token | users created |
|---|---|---|---|
| always-fail `2x0000…AA` | 403 | 403 | 0 |
| always-pass `1x0000…AA` | 403 | 202 | 1 |
| disabled (empty) | 202 | 202 | 2 |

## Deploy

Requires two secrets on the production host, in `/root/UptimeBot/.env`:

```
TURNSTILE_SITE_KEY=0x4AAAAAAA...
TURNSTILE_SECRET_KEY=0x4AAAAAAA...
```

Create the widget at Cloudflare Dashboard → Turnstile → Add widget
(domains: `checkpulse.dev`, `www.checkpulse.dev`; mode: Managed). The API
route needs an API token with the **Account › Turnstile › Edit** permission;
the current `CLOUDFLARE_API_TOKEN` in `~/.hermes/cloudflare.env` returns
`10000 Authentication error` for that endpoint.

Then: `docker compose up -d --build api`

## Still outstanding (not in this change)

1. **Purge the 1,192 junk users** so activation/conversion metrics mean
   something. Keep the 2 real accounts.
2. **Check `checkpulse.dev` sending reputation** in Google Postmaster Tools,
   and confirm SPF/DKIM/DMARC are present. Three months of unsolicited mail
   may be suppressing real customer alert delivery.
3. **Rotate the CheckPulse root password** (pasted in chat, flagged
   2026-09-15, still not rotated). Key auth via `~/.ssh/elvis` already works.
