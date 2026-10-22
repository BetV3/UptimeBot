#!/usr/bin/env python3
"""Print CheckPulse signup-funnel counts from Redis.

Usage (from the prod host, or via ssh):
    docker exec -e PYTHONPATH=/code uptimebot-api-1 python scripts/funnel.py [days]

Output: one row per UTC day with a column per signup step, then totals, the
step-to-step conversion over the window, and where new accounts came from.
Counts are aggregate only (see app/core/funnel.py); there is nothing
per-person to show.
"""
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

from redis import Redis

from app.core.config import get_settings
from app.core.funnel import KEY_PREFIX, STEPS

# Column headers short enough for a phone-width terminal.
SHORT = {
    "register_submit": "submit",
    "turnstile_failed": "captcha",
    "validation_failed": "invalid",
    "account_created": "created",
    "email_verified": "verified",
    "first_project_created": "project",
    "first_monitor_created": "monitor",
    "checkout_started": "checkout",
}


def collect(r, days: int, today=None) -> tuple[list[tuple[str, dict]], Counter, Counter]:
    """Read the last ``days`` daily hashes: (rows, step totals, account sources)."""
    today = today or datetime.now(timezone.utc).date()
    rows, totals, sources = [], Counter(), Counter()
    for i in range(days - 1, -1, -1):
        day = (today - timedelta(days=i)).isoformat()
        h = {(k.decode() if isinstance(k, bytes) else k): int(v)
             for k, v in r.hgetall(KEY_PREFIX + day).items()}
        counts = {s: h.get(s, 0) for s in STEPS}
        totals.update(counts)
        for field, n in h.items():
            step, sep, src = field.partition("|")
            if sep and step == "account_created":
                sources[src] += n
        rows.append((day, counts))
    return rows, totals, sources


def render(days: int, rows, totals, sources) -> str:
    """Format the report as plain text."""
    out = [f"signup funnel, last {days} days (UTC), aggregate counts only"]
    out.append("  day         " + " ".join(f"{SHORT[s]:>8}" for s in STEPS))
    for day, counts in rows:
        out.append(f"  {day}  " + " ".join(f"{counts[s]:8d}" for s in STEPS))
    out.append("  total       " + " ".join(f"{totals[s]:8d}" for s in STEPS))
    out.append("conversion:")
    path = ["register_submit", "account_created", "email_verified",
            "first_project_created", "first_monitor_created", "checkout_started"]
    for a, b in zip(path, path[1:]):
        pct = f"{100 * totals[b] / totals[a]:.0f}%" if totals[a] else "n/a"
        out.append(f"  {SHORT[a]:>8} -> {SHORT[b]:<8} {pct:>5}  ({totals[b]}/{totals[a]})")
    out.append("accounts by source:")
    if not sources:
        out.append("      0  (none)")
    for src, n in sources.most_common(15):
        out.append(f"  {n:5d}  {src}")
    return "\n".join(out)


def main(days: int) -> None:
    """Connect to the configured Redis and print the report."""
    r = Redis.from_url(get_settings().redis_url, decode_responses=True)
    rows, totals, sources = collect(r, days)
    print(render(days, rows, totals, sources))


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 14)
