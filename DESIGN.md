# CheckPulse - Landing Page Design Prompt

Use this prompt with Claude, ChatGPT, or any AI tool to generate the landing page.
Copy everything below the line.

---

Design and build a single-page landing page for "CheckPulse" as a fully self-contained HTML file with inline CSS and JavaScript. No external frameworks except Google Fonts. The page must be responsive and production-ready.

## What the product is

CheckPulse is a reliability and incident-communication platform for monetized Discord and Telegram bots. It monitors bot-specific health signals (not just HTTP pings), auto-creates and communicates incidents to communities, correlates failures with Discord/Telegram platform outages, and gives bot creators a public "trust page" so users stop asking "is the bot down?"

Target audience: Discord bot developers with 75+ guilds or premium features, and Telegram bot developers selling digital goods via Stars.

## Design direction

### Overall vibe
Dark, technical, trustworthy. Think: a mission control dashboard for bot reliability. The aesthetic should say "we take your bot's uptime as seriously as you do." Not playful or bubbly. Not corporate enterprise. Somewhere between a dev tool and an operations platform.

### Color palette
- Background: deep navy/dark blue-black (#0A0E1A or similar), not pure black
- Primary accent: electric violet/purple (#7C3AED) -- this is the brand color
- Secondary accent: cyan/teal (#06B6D4) for secondary highlights and data visualization
- Status colors: green (#10B981) for operational, amber (#F59E0B) for degraded, red (#EF4444) for down
- Text: white (#F8FAFC) for headings, muted gray (#94A3B8) for body text
- Cards/surfaces: slightly lighter than background (#111827 or #1E293B) with subtle border (#1E293B)

### Typography
- Headings: Inter or JetBrains Mono (monospace for that dev-tool feel)
- Body: Inter
- Code snippets and technical text: JetBrains Mono

### Visual style
- Subtle grid or dot pattern on the background (very faint, like a blueprint or terminal grid)
- Glow effects on accent elements (subtle purple/cyan glow behind key UI elements)
- Cards with very subtle glassmorphism (backdrop-blur with low-opacity borders)
- Smooth scroll animations as sections enter viewport
- No stock photos. Use SVG illustrations, code snippets, and UI mockups instead
- Status indicator dots (green/amber/red) used as visual motifs throughout

## Page sections (in order)

### 1. Navigation
Sticky top nav, transparent background that gains a slight blur on scroll. Logo on left ("Check" in bold, "Pulse" in accent color). Nav links: Features, Pricing, Docs. CTA button on right: "Start Free" with purple background.

### 2. Hero
Left side: text content. Right side: a stylized mockup of the dashboard or a terminal-style animation.

Headline: "Your bot goes down. Your users complain. You find out last."
Subheadline: "CheckPulse detects the failures your users actually feel and communicates incidents automatically -- before your community notices."

Two CTA buttons:
- Primary (purple): "Start Monitoring Free"
- Secondary (outlined): "See How It Works"

Below the CTAs, a subtle trust line: "Free forever for 1 bot. No credit card required."

The right side should show either:
- A dark-themed mockup of a trust page showing bot status (operational, with green dots, uptime percentages, and an incident timeline)
- OR a terminal-style animation showing check results flowing in from US/EU/Asia regions

### 3. Problem statement
Heading: "Generic monitoring tools don't understand bots."

Three columns, each with an icon and short description:
1. "They check if a URL returns 200. They don't know if your slash commands are timing out."
2. "They send you an email. They don't post an incident embed in your support server."
3. "They can't tell the difference between your bot failing and Discord itself being down."

### 4. Feature grid
Heading: "Built for how bots actually break."

Six feature cards in a 3x2 grid (2x3 on mobile):

Card 1 - "Bot-Native Health Checks"
Monitor interaction ack latency, gateway heartbeat health, command success rates, and webhook delivery -- not just HTTP status codes.

Card 2 - "Metrics SDK"
Drop a few lines of code into your bot. We track command success rates, response times, and processing lag from the inside.
Include a small code snippet:
```python
import checkpulse
checkpulse.track_command("/play", latency_ms=120, success=True)
```

Card 3 - "Auto Incident Communication"
When something breaks, we push rich Discord embeds to your support server, update your trust page, and notify via Telegram, email, or webhook. Automatically.

Card 4 - "Platform Correlation"
We monitor Discord and Telegram's own infrastructure. When the platform is degraded, we annotate your incidents so users stop blaming your bot.

Card 5 - "Trust Pages"
A public page your community can check instead of DMing you. Shows real-time status, 90-day uptime, incident history, and platform context.

Card 6 - "Multi-Region Checks"
Every check runs from US, Europe, and Asia. We only alert when 2+ regions confirm the problem. No more false alarms from one flaky route.

### 5. How it works
Heading: "60 seconds to your first monitor."

Three steps displayed horizontally (vertically on mobile) with connecting lines/arrows between them:

Step 1: "Register your bot" -- Pick Discord or Telegram, name your bot, get your API key.
Step 2: "Add monitors + alerts" -- Paste your health endpoint, connect your Discord webhook, set thresholds.
Step 3: "Install the SDK (optional)" -- Add 3 lines to your bot for command-level observability.

### 6. Live demo / mockup section
Heading: "What your trust page looks like"

Show a realistic mockup of a trust page with:
- Bot name and logo at top
- Overall status: "All Systems Operational" with green indicator
- Three monitors listed (Bot API, Gateway Shard 0, Command Handler) each with green status dot and "99.97%" uptime
- A 90-day uptime bar (mostly green with a tiny red blip)
- A "Recent Incidents" section with one resolved incident
- A banner showing "Discord API: Operational" with a subtle Discord logo

This should be rendered as an actual styled div, not an image.

### 7. Pricing
Heading: "Simple pricing. Scale when you're ready."

Three pricing cards side by side:

Free ($0/month):
- 1 bot
- 3 monitors
- 5-minute intervals
- Basic trust page
- Discord + email alerts
- 7-day history
- CTA: "Start Free"

Pro ($12/month):
- 3 bots
- 15 monitors
- 1-minute intervals
- All monitor types + SDK
- Platform correlation
- Custom domain
- 90-day history
- CTA: "Start Pro" (this card should be visually highlighted as recommended)

Studio ($49/month):
- 10 bots
- 50 monitors
- 30-second intervals
- Team access (5 seats)
- Branded trust pages
- Priority support
- 1-year history
- CTA: "Start Studio"

Below pricing: "All plans include multi-region monitoring and SSL. Cancel anytime."

### 8. Social proof / credibility
Since this is pre-launch, use a credibility section instead of testimonials:

Heading: "Built by infrastructure engineers for bot creators."

A few trust signals:
- "Checks from 3 continents"
- "Sub-60-second incident detection"
- "Discord and Telegram native"
- "Open-source SDK"

Display these as a horizontal row of badges/pills with subtle icons.

### 9. Final CTA
Dark card with purple gradient border/glow.

Heading: "Your bot deserves better than 'is it down?'"
Subtext: "Start monitoring in under 60 seconds. Free forever for 1 bot."
Single large CTA button: "Get Started Free"

### 10. Footer
Minimal footer with:
- Logo
- Links: Features, Pricing, Docs, Status, GitHub
- "Built with care in Texas."
- Copyright line

## Technical requirements

- Single HTML file, fully self-contained
- All CSS inline in a <style> tag (no external CSS files)
- All JS inline in a <script> tag (no external JS files)
- Only external dependency: Google Fonts (Inter + JetBrains Mono)
- Responsive: looks good on mobile (375px), tablet (768px), and desktop (1440px)
- Smooth scroll behavior for anchor links
- Intersection Observer for fade-in animations as sections scroll into view
- Nav background blur effect on scroll
- Accessible: proper heading hierarchy, alt text, sufficient contrast
- Performance: no heavy animations, no large assets

## What NOT to do

- No gradients everywhere. Use them sparingly (CTA card, hero accent).
- No rounded bubbly cards. Use sharp or slightly rounded corners (4-8px max).
- No emoji in headings.
- No "AI-powered" or "revolutionary" or "game-changing" language.
- No stock photography.
- No light mode. This is a dark-themed site only.
- Don't use more than 2 font weights for body text.
- Don't make it look like a generic SaaS template. It should feel like a tool built by developers, for developers.
