Yes. Before you market, you want to know whether your DNS and SSL are **boringly correct**. Nobody buys monitoring from a site that can’t keep its own domain and certificate situation under control. Very embarrassing genre.

Below is an **extensive DNS and SSL test pack** for **CheckPulse**. It covers:

* marketing site
* app subdomain
* API/webhook subdomain
* email-sending subdomain
* redirects
* certificates
* browser trust
* renewal safety
* security headers tied to SSL

Use it as a **pre-launch checklist**.

---

# Scope to test

List every hostname you actually use.

Example inventory:

* `checkpulse.com`
* `www.checkpulse.com`
* `app.checkpulse.com`
* `api.checkpulse.com`
* `notify.checkpulse.com` or `notifications.checkpulse.com`
* any webhook/admin/staging hostnames you expose publicly

If you do not have this inventory written down, fix that first.

---

# Severity levels

## P0

Must pass before marketing:

* domain resolves correctly
* `https` works
* cert is valid and trusted
* no broken redirect loops
* no mixed content on key pages
* app/login/billing all load over HTTPS
* email/auth links point to the correct domain

## P1

Should fix before serious traffic:

* missing HSTS
* missing CAA
* weak TLS config
* inconsistent apex/www behavior
* IPv6 broken while IPv4 works
* stale DNS records from old providers

## P2

Nice to improve:

* DNS TTL tuning
* secondary vanity redirects
* prettier certificate chain hygiene
* optional advanced TLS polish

---

# Part 1: DNS testing

## Test 1: Apex domain resolves correctly

### Goal

`checkpulse.com` resolves to the intended target.

### Steps

Run:

```bash
dig checkpulse.com
dig A checkpulse.com
dig AAAA checkpulse.com
```

### Expected

* A record exists if using IPv4
* AAAA exists if using IPv6 intentionally
* records point to the correct platform/IP/provider
* no old provider IPs remain

### Failures

* NXDOMAIN
* wrong IP/provider
* only one family works when you intended both
* stale records from a prior deployment

---

## Test 2: `www` resolves correctly

### Goal

`www.checkpulse.com` resolves and behaves intentionally.

### Steps

```bash
dig www.checkpulse.com
dig A www.checkpulse.com
dig AAAA www.checkpulse.com
```

Then test in browser:

* `http://www.checkpulse.com`
* `https://www.checkpulse.com`

### Expected

Choose one canonical behavior:

* either `www` redirects to apex
* or apex redirects to `www`

Do **not** leave both as first-class copies unless you want duplicate-indexing nonsense.

---

## Test 3: App subdomain resolves correctly

### Goal

`app.checkpulse.com` resolves consistently.

### Steps

```bash
dig app.checkpulse.com
dig A app.checkpulse.com
dig AAAA app.checkpulse.com
```

Open:

* `http://app.checkpulse.com`
* `https://app.checkpulse.com`

### Expected

* app hostname resolves correctly
* HTTP redirects to HTTPS
* no cross-domain weirdness from the marketing site

---

## Test 4: API subdomain resolves correctly

### Goal

If you expose `api.checkpulse.com`, it resolves correctly.

### Steps

```bash
dig api.checkpulse.com
curl -I https://api.checkpulse.com
```

### Expected

* DNS resolves
* TLS is valid
* response is intentional
* if browser access is not meant for humans, you still return a sane response and valid certificate

---

## Test 5: Email subdomain resolves correctly

### Goal

Your Resend sending subdomain is configured properly.

Example:

* `notify.checkpulse.com`

### Steps

```bash
dig notify.checkpulse.com
dig TXT notify.checkpulse.com
```

### Expected

* domain exists if needed
* required provider verification records are present
* SPF/DKIM/other mail auth records are set where appropriate

This matters because broken mail DNS turns account verification and billing emails into a trash fire.

---

## Test 6: Nameserver sanity

### Goal

Your domain is using the correct authoritative nameservers.

### Steps

```bash
dig NS checkpulse.com
whois checkpulse.com
```

### Expected

* nameservers match your DNS provider
* no lingering old nameservers
* registrar and DNS setup are consistent

---

## Test 7: CNAME chains are sane

### Goal

Subdomains do not resolve through a ridiculous chain.

### Steps

```bash
dig app.checkpulse.com
dig www.checkpulse.com
```

### Expected

* short, intentional chain
* no broken CNAME targets
* no loop
* no mix of old and new providers

---

## Test 8: DNS propagation from public resolvers

### Goal

Major resolvers see the same records.

### Steps

Test from multiple resolvers:

```bash
dig @1.1.1.1 checkpulse.com
dig @8.8.8.8 checkpulse.com
dig @9.9.9.9 checkpulse.com
dig @1.1.1.1 app.checkpulse.com
dig @8.8.8.8 app.checkpulse.com
```

### Expected

* answers are consistent
* no resolver sees old IPs
* TTLs make sense

---

## Test 9: TTL sanity

### Goal

TTL values are intentional.

### Steps

```bash
dig checkpulse.com
dig www.checkpulse.com
dig app.checkpulse.com
```

### Expected

* not absurdly low forever
* not absurdly high during launch if you may change infra
* common sane range for launch: roughly 300 to 3600 seconds depending on stability

### Bad sign

TTL of 30 seconds on everything forever because “fast changes.” That is just extra DNS load and chaos.

---

## Test 10: No orphaned/stale records

### Goal

Old records are removed.

### Check for:

* old A records
* old CNAMEs
* test/staging domains publicly exposed
* obsolete MX/TXT/SPF records
* dead verification records you no longer need

### Expected

Only active records remain publicly exposed.

---

## Test 11: CAA records

### Goal

Only approved certificate authorities can issue certs for your domain.

### Steps

```bash
dig CAA checkpulse.com
dig CAA app.checkpulse.com
```

### Expected

* CAA exists if you want stricter control
* points to your intended CA(s)

This is not mandatory for launch, but it is good hygiene.

---

## Test 12: MX, SPF, DKIM, DMARC

### Goal

Mail-related DNS is sane for transactional emails.

### Steps

```bash
dig MX checkpulse.com
dig TXT checkpulse.com
dig TXT _dmarc.checkpulse.com
```

Also check the Resend-provided DKIM/SPF records on the sending domain.

### Expected

* SPF exists and is not broken by multiple SPF records
* DKIM is configured for sending domain
* DMARC exists, even if relaxed to start
* no contradictory mail records

---

## Test 13: Wildcard exposure check

### Goal

You are not accidentally serving unknown subdomains.

### Steps

Try:

```bash
dig randomgarbage123.checkpulse.com
```

Open in browser if it resolves.

### Expected

* either NXDOMAIN
* or intentional wildcard behavior

### Bad sign

Every random subdomain resolves to production without you meaning to.

---

## Test 14: IPv4/IPv6 parity

### Goal

If you publish AAAA records, IPv6 actually works.

### Steps

```bash
curl -4 -I https://checkpulse.com
curl -6 -I https://checkpulse.com
curl -4 -I https://app.checkpulse.com
curl -6 -I https://app.checkpulse.com
```

### Expected

* both work if AAAA exists
* if IPv6 is broken, remove AAAA until fixed

Broken IPv6 is a classic self-own.

---

# Part 2: SSL/TLS testing

## Test 15: Certificate validity on apex

### Goal

`https://checkpulse.com` presents a valid cert.

### Steps

In browser:

* open `https://checkpulse.com`
* inspect certificate

Command line:

```bash
openssl s_client -connect checkpulse.com:443 -servername checkpulse.com
```

### Expected

* cert is valid
* hostname matches
* not expired
* trusted by browser
* full chain is served

---

## Test 16: Certificate validity on `www`

### Goal

`https://www.checkpulse.com` also presents a valid cert.

### Steps

```bash
openssl s_client -connect www.checkpulse.com:443 -servername www.checkpulse.com
```

### Expected

* valid cert
* SAN covers `www`
* no mismatch or fallback cert

---

## Test 17: Certificate validity on app

### Goal

`https://app.checkpulse.com` has a correct cert.

### Steps

```bash
openssl s_client -connect app.checkpulse.com:443 -servername app.checkpulse.com
```

### Expected

* hostname matches
* chain valid
* no staging/default certificate leakage

---

## Test 18: Certificate coverage for all public hosts

### Goal

Every public hostname has coverage.

### Check these explicitly:

* apex
* `www`
* `app`
* `api`
* any auth/billing callback hostnames
* any public docs/status/tool subdomains

### Expected

Every public host either:

* has its own valid cert
* or is covered by wildcard/SAN intentionally

---

## Test 19: HTTP to HTTPS redirect behavior

### Goal

All public site traffic upgrades cleanly.

### Steps

```bash
curl -I http://checkpulse.com
curl -I http://www.checkpulse.com
curl -I http://app.checkpulse.com
```

### Expected

* clean 301/308 redirect to HTTPS
* no loops
* no redirecting to the wrong hostname

---

## Test 20: Redirect chain sanity

### Goal

No ugly multi-hop redirect chain.

### Steps

Use browser dev tools or:

```bash
curl -I -L http://checkpulse.com
```

### Expected

* ideally 1 to 2 redirects max
* ends on canonical HTTPS host

### Bad sign

`http -> https -> www -> apex -> app -> slash variation nonsense`

---

## Test 21: TLS protocol support

### Goal

Only sane TLS versions are enabled.

### What to check

Prefer:

* TLS 1.2
* TLS 1.3

Avoid:

* TLS 1.0
* TLS 1.1

### Tools

Use SSL Labs or test with OpenSSL/curl depending on your infra.

### Expected

Modern protocol support only.

---

## Test 22: Weak cipher / weak config check

### Goal

No obviously weak SSL configuration.

### Test with

* SSL Labs
* your reverse proxy config review
* Mozilla SSL config generator target if relevant

### Expected

* no weak ciphers
* no ancient fallback junk
* modern forward-secret defaults if applicable

---

## Test 23: Certificate chain completeness

### Goal

Intermediate certs are served properly.

### Steps

```bash
openssl s_client -connect checkpulse.com:443 -servername checkpulse.com -showcerts
```

### Expected

* full chain present
* browsers trust it cleanly
* no “works on my machine but not on Android/older clients” nonsense

---

## Test 24: Expiry monitoring

### Goal

You know before certs expire.

### Steps

Check expiry date manually for all public hosts.

### Expected

* all certs valid well beyond launch
* you have reminders/automation in place
* your own product should probably monitor these, which would be poetic at least

---

## Test 25: Browser trust matrix

### Goal

Site is trusted across major browsers/devices.

### Test on:

* Chrome desktop
* Firefox desktop
* Safari if available
* iPhone/Android if possible

### Expected

* no trust warnings
* no interstitials
* no partial-secure warnings

---

## Test 26: Mixed content check

### Goal

HTTPS pages do not load insecure assets.

### Steps

Open homepage, pricing, signup, login, dashboard in browser dev tools.

### Expected

* no mixed content warnings
* all scripts/images/fonts/styles load over HTTPS
* no insecure API calls

This one kills trust fast.

---

## Test 27: Secure cookies

### Goal

Auth/session cookies are safe under HTTPS.

### Check

In browser dev tools for auth cookies:

* `Secure`
* `HttpOnly` where appropriate
* `SameSite` intentionally set

### Expected

Sensitive cookies should not be casually exposed.

---

## Test 28: HSTS header

### Goal

Browsers are told to prefer HTTPS.

### Steps

```bash
curl -I https://checkpulse.com
curl -I https://app.checkpulse.com
```

### Expected

`Strict-Transport-Security` present on production hosts

### Good starting point

Something like:

* `max-age=31536000; includeSubDomains`
  Only preload if you actually know what you’re doing.

---

## Test 29: Security headers related to secure delivery

### Check for

* `Strict-Transport-Security`
* `Content-Security-Policy`
* `X-Content-Type-Options`
* `Referrer-Policy`
* `Permissions-Policy`
* `X-Frame-Options` or CSP frame control

### Expected

At least baseline sane security headers exist.

---

## Test 30: TLS on billing flow

### Goal

Stripe-related redirects and returns are clean.

### Steps

* start upgrade flow
* go to Checkout
* return from success URL
* return from cancel URL

### Expected

* all return URLs are HTTPS
* no certificate issues
* no callback to wrong host
* no auth/session breakage due to domain mismatch

---

## Test 31: Auth email link domain correctness

### Goal

Verification/reset/login emails use correct HTTPS links.

### Steps

Trigger:

* signup verification email
* password reset email
* billing-related email if applicable

### Expected

* links point to production HTTPS domain
* no localhost
* no wrong subdomain
* no expired/mismatched cert on landing page

---

## Test 32: Webhook endpoint TLS

### Goal

Your Stripe webhook endpoint is HTTPS-valid.

### Steps

Test the public webhook path:

```bash
curl -I https://yourdomain.com/api/stripe/webhook
```

### Expected

* valid TLS
* reachable from Stripe
* no certificate mismatch
* no HTTP-only exposure

---

## Test 33: API CORS + HTTPS behavior

### Goal

Frontend calls over HTTPS work cleanly.

### Steps

Use browser dev tools on:

* signup
* login
* add monitor
* billing
* settings

### Expected

* API calls use HTTPS
* no mixed content
* no CORS failures from wrong origin settings

---

## Test 34: Mobile SSL behavior

### Goal

Mobile browsers trust and load the site correctly.

### Steps

Open:

* homepage
* login
* dashboard
* pricing
* checkout return pages

### Expected

* no warnings
* no broken secure assets
* no cookie/session weirdness

---

## Test 35: Status and tools pages if public

### Goal

Any public tool pages also pass DNS/SSL.

Check:

* free SSL checker page
* DNS checker page
* status page
* docs pages
* blog pages

### Expected

All public entry points are equally clean.

---

# Automated / recurring tests you should set up

## Daily automated DNS checks

* apex A/AAAA
* `www`
* `app`
* `api`
* mail subdomain records
* MX/SPF/DKIM/DMARC presence

## Daily automated SSL checks

* expiry window
* hostname match
* chain validity
* HSTS presence on main hosts
* HTTP->HTTPS redirect status

## Weekly deeper checks

* SSL Labs scan
* security headers scan
* public resolver propagation comparison
* stale-record audit after any infra change

---

# Suggested command checklist

Use these as your basic shell pack:

```bash
dig checkpulse.com
dig www.checkpulse.com
dig app.checkpulse.com
dig api.checkpulse.com
dig MX checkpulse.com
dig TXT checkpulse.com
dig TXT _dmarc.checkpulse.com
dig CAA checkpulse.com

curl -I http://checkpulse.com
curl -I https://checkpulse.com
curl -I http://www.checkpulse.com
curl -I https://www.checkpulse.com
curl -I https://app.checkpulse.com
curl -4 -I https://checkpulse.com
curl -6 -I https://checkpulse.com

openssl s_client -connect checkpulse.com:443 -servername checkpulse.com
openssl s_client -connect www.checkpulse.com:443 -servername www.checkpulse.com
openssl s_client -connect app.checkpulse.com:443 -servername app.checkpulse.com
```

---

# Minimum must-pass before marketing

Do not start real promotion until these pass:

* apex resolves correctly
* `www` canonical redirect works
* app subdomain resolves correctly
* all public hosts serve valid certs
* HTTP always redirects to HTTPS
* no mixed content on homepage, signup, login, dashboard, billing
* auth/billing email links point to correct HTTPS domain
* HSTS present on main production hosts
* Resend sending domain records are valid
* Stripe webhook endpoint is HTTPS-valid

---

# What to log for each test

For every failure, track:

* hostname
* test name
* expected result
* actual result
* screenshot or command output
* severity
* owner
* fixed date

---

# Best next step

If you want, I can turn this into either:

1. a **QA spreadsheet/checklist format** you can copy into Sheets, or
2. a **domain-specific test plan** if you paste your actual hostnames like:

   * apex
   * `www`
   * app
   * API
   * email subdomain
   * status/docs/tool subdomains

The second option is better because then I can make the checklist match your actual setup instead of the usual placeholder-domain theater.
