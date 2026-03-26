# CheckPulse Landing Page Copy


## Hero Section

### Headline
**Your bot goes down. Your users complain. You find out last.**

### Subheadline
CheckPulse monitors your Discord and Telegram bots from 3 continents and alerts you instantly -- before your community even notices.

### CTA Button
**Start Monitoring Free** (no credit card required)

### Social Proof Line
Trusted by bot developers running bots with 10K+ server installs.
*(Update this as you get real numbers. Until then, omit it.)*


---


## Problem Section

### Heading
**You built the bot. Who's watching it?**

Your bot serves thousands of users across hundreds of servers. When it goes down at 3am, you don't find out until you wake up to a flooded support channel.

Generic monitoring tools like UptimeRobot weren't built for bot developers. They'll tell you a URL returned 200, but they won't:

- Alert your Discord server the way your community expects
- Check from multiple regions so you know if it's really down
- Give your Patreon supporters a status page they can actually trust
- Let you manage monitors without leaving Discord


---


## Features Section

### Heading
**Monitoring that speaks bot developer**

#### Multi-Region Checks
Your bot gets pinged from the US, Europe, and Asia every 60 seconds. We only alert when 2+ regions confirm it's down -- no more 3am false alarms from a single flaky check.

#### Discord-Native Alerts
Not a plain text webhook. Rich embeds with status, response time, and incident duration -- dropped right into your ops channel. Looks like it belongs there because it does.

#### Public Status Pages
Give your community a live status page at `status.checkpulse.dev/your-bot`. Embed a status widget in your Discord server's info channel. Show your Patreon supporters you take reliability seriously.

#### Telegram Alerts
Running a Telegram bot? Get instant notifications in your Telegram chat. Manage monitors with inline commands.

#### 30-Second Setup
Paste your bot's health endpoint. Pick your Discord webhook. Done. No 20-minute onboarding flow, no enterprise sales call, no YAML files.

#### Incident Timeline
When something breaks, you get a clear timeline: when it went down, from which regions, how long it was out, and when it recovered. Export it for your post-mortem or share it with your team.


---


## How It Works Section

### Heading
**Three steps. Under a minute.**

**1. Add your endpoint**
Paste the URL your bot's health check lives at. We support GET and POST with custom headers if you need them.

**2. Connect your alerts**
Drop in your Discord webhook URL, Telegram bot token, or email. Or all three.

**3. Relax**
We check your bot every 60 seconds from 3 regions. If something breaks, you'll know before your users do.


---


## Pricing Section

### Heading
**Simple pricing. No per-monitor math.**

#### Free -- $0/month
- 3 monitors
- 5-minute check intervals
- 1 status page
- Discord + email alerts
- Checked from 3 regions

*Perfect for side projects and hobby bots.*

#### Starter -- $5/month
- 10 monitors
- 1-minute check intervals
- 3 status pages
- Discord, Telegram, and email alerts
- Checked from 3 regions
- Incident history (90 days)

*For bot developers who take uptime seriously.*

#### Pro -- $15/month
- 50 monitors
- 30-second check intervals
- Unlimited status pages
- All alert channels
- Custom status page domain
- Priority support

*For teams and developers running multiple production bots.*

### Below Pricing
All plans include multi-region monitoring, SSL, and zero setup fees. Cancel anytime.


---


## Comparison Section

### Heading
**How CheckPulse stacks up**

| Feature | CheckPulse Starter | UptimeRobot Solo | Uptime Kuma |
|---|---|---|---|
| Price | $5/mo | $7/mo | Free (self-hosted) |
| Monitors | 10 | 10 | Unlimited |
| Check interval | 1 min | 1 min | 1 min |
| Multi-region checks | 3 regions | Single region | Single instance |
| Discord-native alerts | Rich embeds | Basic webhook | Basic webhook |
| Hosted (zero maintenance) | Yes | Yes | No (you manage it) |
| Status pages | Included | Included | Included |
| Built for bot developers | Yes | No | No |

*Uptime Kuma is great, but if your monitoring tool runs on the same server as your bot, they go down together.*


---


## FAQ Section

### Who is CheckPulse for?
CheckPulse is built for developers who run Discord bots, Telegram bots, or APIs that serve online communities. If your users rely on your bot being online and you want to know the second it's not, this is for you.

### How is this different from UptimeRobot?
UptimeRobot is a general-purpose monitoring tool. CheckPulse is purpose-built for bot developers. That means Discord-native alerts with rich embeds, multi-region verification to eliminate false alarms, and status pages designed to embed in your Discord server. Also, our Starter plan is $5/mo vs their $7/mo.

### How is this different from Uptime Kuma?
Uptime Kuma is a self-hosted, single-instance tool. If the server running it goes down, your monitoring goes down with it. CheckPulse runs in the cloud and checks from 3 continents, so you have redundancy built in. No server to maintain, no Docker to manage, no updates to apply.

### What endpoints can I monitor?
Any HTTP or HTTPS endpoint. Most bot developers point us at their `/health` or `/status` endpoint. We support custom headers, expected status codes, and POST requests with bodies.

### Do you really check from 3 continents?
Yes. Every check runs from the US (East Coast), Europe (Frankfurt), and Asia (Singapore). We only flag an incident when 2 or more regions confirm the endpoint is down, which eliminates false positives from regional network blips.

### Can I use this for non-bot projects?
Absolutely. CheckPulse works for any API or web service. But our features, integrations, and community are optimized for bot developers.


---


## Footer CTA

### Heading
**Your bot deserves better than "is it down?"**

Start monitoring in under 60 seconds. Free forever for up to 3 monitors.

**[Start Monitoring Free]**


---
---


# CheckPulse Marketing Strategy


## Target Audience

**Primary:** Discord bot developers who monetize their bots (Patreon, premium tiers, server subscriptions). They have paying users who expect reliability but don't have enterprise monitoring budgets.

**Secondary:** Telegram bot developers, especially in fintech/crypto niches where uptime directly impacts trust and revenue.

**Tertiary:** Self-hosters running Uptime Kuma who are frustrated with single-instance limitations and want something managed.


---


## Positioning Statement

CheckPulse is the uptime monitoring service built specifically for bot developers. Unlike generic tools like UptimeRobot, CheckPulse integrates natively with Discord and Telegram, checks from 3 regions to eliminate false alarms, and provides status pages designed for online communities. Unlike self-hosted tools like Uptime Kuma, CheckPulse requires zero maintenance and never goes down with your server.


---


## Pre-Launch (Weeks 1-6, while building)

### Build in Public
- Post weekly progress updates on Twitter/X with the hashtag #buildinpublic
- Share screenshots of the dashboard, status pages, and Discord alert embeds as you build them
- Be transparent about architecture decisions (homelab + VPS workers is a great story)
- Share your build plan and weekly progress in a short thread format

### Create a Waitlist
- Simple landing page with the hero section copy above
- Email capture: "Get early access + 3 months free on Starter when we launch"
- Use a free tool like Buttondown or Mailchimp free tier
- Drop the waitlist link in relevant communities (see distribution below)

### Seed Content
- Write 2-3 short posts for dev.to or your personal blog:
  - "Why I'm building a monitoring tool specifically for Discord bots"
  - "The problem with monitoring your bot from a single location"
  - "I'm running my SaaS on a homelab -- here's why"
- These double as SEO seeds and community discussion starters


---


## Launch Week (Week 8-9)

### Day 1: Soft Launch
- Announce in 2-3 Discord bot development servers you're already a member of
- Don't spam. Frame it as: "I built this because I had this problem. Looking for 10 beta testers to try it free."
- DM developers you've interacted with in those communities

### Day 2: Reddit
Post on these subreddits (one per day, don't carpet bomb):
- r/discord_bots -- "I built a free uptime monitor specifically for Discord bots"
- r/discordapp -- share as a tool/resource
- r/selfhosted -- position against Uptime Kuma: "I built a hosted alternative to Uptime Kuma for bot developers"
- r/SideProject -- share the build story
- r/webdev -- if it fits the community vibe

Format: Tell a story, not a sales pitch. "I run a Discord bot with X servers. I got tired of finding out it was down from user complaints. So I built this."

### Day 3-4: Twitter/X
- Launch thread: problem, solution, demo GIF, link
- Tag bot framework accounts (@disclojs, etc.) and bot developers you admire
- Share a 30-second screen recording of the setup flow

### Day 5: Hacker News
- Post as "Show HN: CheckPulse -- Uptime monitoring built for Discord/Telegram bot developers"
- Be available to answer every comment for the first 6 hours
- HN loves technical founder stories -- lean into the homelab architecture angle

### Day 7: Product Hunt (optional)
- Only do this if you have a polished landing page and dashboard
- Get 5-10 people to leave reviews on launch day
- Prepare screenshots, a short video, and a clear one-liner


---


## Post-Launch Growth (Months 2-6)

### Community Presence (ongoing, 30 min/day)
- Be active in Discord bot development servers. Answer questions. Help people debug their bots. Don't pitch CheckPulse unless it's genuinely relevant.
- People buy from people they recognize and trust. Become a known name in 3-4 Discord bot dev communities before you ever pitch.
- When someone asks "how do I know if my bot went down?" -- that's your moment.

### Content Marketing (1 post every 2 weeks)
Write practical content that bot developers search for:
- "How to add a health check endpoint to your Discord.js bot"
- "Setting up uptime monitoring for your Discord bot (free)"
- "What to do when your Discord bot goes down at 3am"
- "How to create a status page for your Discord bot"
- "Discord bot uptime: why checking from one location isn't enough"

Publish on: dev.to, your blog (checkpulse.dev/blog), and cross-post to relevant subreddits. These posts build SEO over time and position you as an authority.

### Bot Listing Sites
- Create a presence on top.gg if you build a CheckPulse Discord bot
- List on botlist.me, discordbotlist.com, and similar directories
- These are high-intent audiences -- people actively looking for bot tools

### Partnerships
- Reach out to bot hosting platforms (Railway, Fly.io, Render) about cross-promotion
- Offer affiliate/referral deals to popular bot framework tutorial creators
- Sponsor a small Discord bot development community's server (costs nothing, just offer free Pro accounts to mods)

### Referral Program
- "Give a friend 3 months free on Starter, get 1 month free yourself"
- Bot developers talk to other bot developers. Word of mouth is your best channel.


---


## Key Metrics to Track

| Metric | Target (Month 3) | Target (Month 6) |
|---|---|---|
| Registered users | 100 | 500 |
| Paying customers | 10 | 50 |
| MRR | $50-75 | $300-500 |
| Monitors active | 200 | 1,000 |
| Status pages created | 30 | 150 |
| Churn rate | Below 10% | Below 5% |

These are realistic targets for a niche SaaS with zero ad spend. The goal isn't hockey-stick growth -- it's consistent, compounding growth from a loyal niche.


---


## Channels to Avoid (for now)

- **Google Ads / paid search**: Too expensive for your budget. "Uptime monitoring" keywords are dominated by well-funded competitors.
- **Facebook/Instagram ads**: Wrong audience. Bot developers don't discover dev tools on Instagram.
- **Cold email**: Don't email bot developers you don't know. It'll hurt your brand.
- **Broad SEO**: Don't try to rank for "uptime monitoring." You'll lose to UptimeRobot and BetterStack. Target long-tail: "discord bot uptime monitoring" and "status page for discord bot."


---


## The One Thing That Matters Most

**Get 10 real users in the first 2 weeks.** Not 10 signups -- 10 people who create a monitor and keep it running. Talk to every one of them. Ask what's missing. Fix the thing they mention most. Those 10 users will tell you more about your product than any amount of planning.

Everything else -- the landing page, the marketing strategy, the pricing -- is just the scaffolding to get those first 10 conversations.