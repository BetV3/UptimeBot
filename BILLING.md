# Billing — Pricing Packaging Update

This round is a **packaging change, not a price change**. Dollar amounts
stay at `$0 / $12 / $49`. What moved is the *feature placement* and the
Pro site limit.

Read this top to bottom before clicking anything in the Stripe dashboard —
a few of the items below are one-way (grandfathering, portal config) and
you want to decide them up front.

---

## 1. What changed in this commit

### Plan limits (enforced in `app/services/plans.py`)

| Plan    | Sites (was → now) | Monitors | Min interval |
|---------|-------------------|----------|--------------|
| Free    | 1 → 1             | 3        | 300s         |
| Starter | 3 → 3             | 10       | 60s          |
| Pro     | **∞ → 10**        | 50       | 30s          |

Only **Pro.max_projects** changed (`None` → `10`). Everything else is
the same number with sharper copy around it.

### Site + dashboard copy (positioning-only, no DB)

- `app/templates/landing.html` — removed "Basic status page" from Free;
  added it to Starter; moved "Custom domain" from Starter to Pro;
  renamed "Branded status pages per client" → "Branded status pages";
  replaced "Unlimited sites" → "10 sites" on Pro.
- `app/api/routes/dashboard.py` — same edits applied to the `tiers`
  list rendered at `/dashboard/billing`.
- `app/templates/billing.html` — usage card label "Bots" → "Sites".

### What did **not** change

- Stripe Price amounts (`STRIPE_STARTER_PRICE_ID` = $12/mo,
  `STRIPE_PRO_PRICE_ID` = $49/mo).
- Stripe Product objects, tax codes, or checkout config.
- `User.plan` enum, `subscription_status` enum, or any migration.
- Webhook handling in `app/api/routes/billing.py`.

So the hot path through Stripe (checkout → webhook → `User.plan`
flip → `PLAN_LIMITS[user.plan]` lookup) is untouched.

---

## 2. Stripe dashboard checklist

Nothing here is load-bearing for the subscription flow — it's all
cosmetic / buyer-facing metadata. But if you leave the old bot-focused
copy in the Stripe checkout header and receipt emails, it will clash
with the new site positioning.

### Products (Stripe → Products)

Open each existing Product (Starter, Pro) and update:

- [ ] **Product name** — keep as "CheckPulse Starter" / "CheckPulse Pro".
      No change unless you want to drop "CheckPulse" prefix in checkout.
- [ ] **Product description** — this shows up in Checkout and the
      billing portal. Replace any bot-focused copy with:
    - *Starter*: "Monitoring for solo operators and small production
      setups. Uptime, DNS, SSL, and regional latency across up to 3
      sites."
    - *Pro*: "Monitoring for agencies and multi-site operators. Up to
      10 sites with branded status pages and custom domains."
- [ ] **Statement descriptor** — confirm it reads `CHECKPULSE` or
      similar. Don't leave it as the default `STRIPE*...`.
- [ ] **Metadata** — optional. If you want a programmatic backstop for
      the plan limits, add `max_sites=3` / `max_monitors=10` etc. on
      each Product so a future migration from env-driven IDs to
      metadata-driven is one query away. Skip this today if you're
      moving fast.

### Prices

- [ ] Open `STRIPE_STARTER_PRICE_ID` — confirm `$12.00 USD / month,
      recurring`. **Do not edit the Price** — Stripe Prices are
      immutable; if you need a different amount, create a new Price
      and swap the env var.
- [ ] Open `STRIPE_PRO_PRICE_ID` — same check, `$49.00 USD / month`.
- [ ] If you want the Price to carry the new packaging label in the
      dashboard, set the Price **nickname** to `starter-v2` /
      `pro-v2-10-sites`. Nicknames are internal-only.

### Customer portal (Stripe → Settings → Billing → Customer portal)

The portal is what `/billing/portal` redirects to. Two things to
confirm *before* the change ships, because they affect existing
customers:

- [ ] **Plan switching** — the portal's "Change plan" section lists
      the Products customers can switch between. If you created new
      Prices (you didn't, in this round), remove the old ones.
- [ ] **Cancellation mode** — confirm "Cancel at period end" is on;
      "Cancel immediately" is usually too aggressive for a
      subscription SaaS.
- [ ] **Invoice history** — enabled.
- [ ] **Customer information** — allow email and payment method edits;
      don't allow name/address edits if you're going to invoice under
      a legal entity later.

### Webhooks

- [ ] No new webhook events needed for this change. The existing
      `customer.subscription.*` and `invoice.*` handlers keep working.
- [ ] Confirm `STRIPE_WEBHOOK_SECRET` still matches the endpoint in
      Stripe → Developers → Webhooks. (You'd know already if it didn't.)

---

## 3. Existing customers — decide before deploy

### The 10-site cap on Pro

Anyone currently on Pro with **>10 projects** would hit a 403 the next
time they try to create a project. `check_project_limit` enforces this
at the API layer; we are not retroactively disabling existing projects.

Options, in order of preference:

1. **Check first.** Run:
   ```sql
   SELECT u.email, u.plan, count(p.id) AS projects
   FROM users u
   JOIN projects p ON p.user_id = u.id
   WHERE u.plan = 'PRO'
   GROUP BY u.id
   HAVING count(p.id) > 10;
   ```
   If this returns 0 rows, you're done. Ship it.
2. **Grandfather.** If someone already has 12 projects, they keep all
   12 — they just can't create #13. That is what the code does today.
   No action needed beyond a heads-up email.
3. **Hard migrate.** Not recommended for this round. Requires a manual
   conversation with each affected customer.

### The moved "Custom domain" feature

This feature is **not currently enforced in code** (per the codebase
survey) — it's a marketing claim on the pricing page, not a gate.

So moving it from Starter → Pro on the site has **no immediate code
effect**. If you want the packaging to actually mean something:

- [ ] Add a `has_custom_domain: bool` field to `PlanLimits` and gate
      the custom-domain setting UI on `PLAN_LIMITS[user.plan].has_custom_domain`.
- [ ] Same for `has_status_page` and `has_branded_status_pages`.

Do this in a follow-up PR; don't bundle it with the packaging change
or the rollback story gets messy.

---

## 4. Verification after deploy

```bash
# Check the tiers render with the new copy + numbers
curl -s https://<host>/ | grep -A2 'pricing-features'
# expect: "10 sites" on Pro, "Basic status page" on Starter, no "Basic
# status page" on Free

# Check the enforced limit
docker compose exec db psql -U uptimebot -d uptimebot -c \
  "SELECT enum_range(NULL::plan_type);"
# expect: {FREE,STARTER,PRO} — no new values needed

# Pretend to be a Pro user, confirm the 11th project is rejected
# (easiest done via the dashboard rather than curl)
```

Then log into Stripe → Customers, pick any active Pro customer, and
confirm the Customer portal link shows the updated Product description.

---

## 5. Rollback

Code-only, no DB:

```
git revert <this-commit>
docker compose build && docker compose up -d api celery-worker
```

Reverts `PLAN_LIMITS[PlanType.PRO].max_projects` back to `None`
(unlimited), restores the old copy on `/` and `/dashboard/billing`.

No Stripe-side revert needed if you only did the Product description
edits — those are idempotent and safe to leave in place even if the
code rolls back. The portal / webhook wiring never changed.
