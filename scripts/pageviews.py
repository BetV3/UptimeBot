#!/usr/bin/env python3
"""Print CheckPulse page-view counts from the prod Redis.

Usage (from the prod host, or via ssh):
    docker exec -e PYTHONPATH=/code uptimebot-api-1 python scripts/pageviews.py [days]

Output: per-day totals, then top pages and top sources over the window.
Counts are aggregate only (see app/core/analytics.py); there is nothing
per-person to show.
"""
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

from redis import Redis

from app.core.analytics import KEY_PREFIX
from app.core.config import get_settings


def main(days: int) -> None:
    r = Redis.from_url(get_settings().redis_url, decode_responses=True)
    today = datetime.now(timezone.utc).date()
    pages, sources, total = Counter(), Counter(), 0
    print(f"page views, last {days} days (UTC), humans only")
    for i in range(days - 1, -1, -1):
        day = (today - timedelta(days=i)).isoformat()
        h = r.hgetall(KEY_PREFIX + day)
        n = sum(int(v) for v in h.values())
        total += n
        for field, v in h.items():
            page, _, src = field.partition("|")
            pages[page] += int(v)
            sources[src] += int(v)
        print(f"  {day}  {n:5d}")
    print(f"total {total}")
    print("top pages:")
    for p, n in pages.most_common(10):
        print(f"  {n:5d}  {p}")
    print("top sources:")
    for s, n in sources.most_common(15):
        print(f"  {n:5d}  {s}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 14)
