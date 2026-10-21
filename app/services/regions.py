"""Check regions: the single source of truth is the ``regions`` table.

Before CP-1 (2026-10-20) regions were a Postgres enum (us/eu/asia), a hardcoded
``REGIONS`` list in the scheduler and a hardcoded ``>= 2`` in two places. Adding
a region meant a migration and a deploy. Now it is one row plus one VPS:

    insert into regions (code, label, city, provider, scheduled, voting)
    values ('eu-central', 'EU Central', 'Frankfurt', 'vultr', true, false);

Two flags, so a new worker can be proven before it gets a say:

- ``scheduled``: the scheduler enqueues work for this region and its workers
  may claim jobs, post results and heartbeat.
- ``voting``: this region's latest *fresh* result counts toward a monitor's
  UP/DOWN verdict. A scheduled-but-not-voting region is a shadow: it builds
  real history next to the voters without being able to open an incident.

The verdict rule (``consensus``) is a strict majority of the voting regions
(3 -> 2, 5 -> 3), and a region's result only counts while it is fresh. The
freshness bound fixes a pre-CP-1 bug: the vote used each region's latest check
with no age limit, so a dead worker's last UP voted forever, and with two
workers dead nothing could ever be marked DOWN.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import text

# A result older than this many intervals no longer votes...
FRESH_INTERVALS = 3
# ...but never less than this, so short-interval monitors tolerate one slow poll.
FRESH_MIN_SECONDS = 15 * 60

CODE_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")

_CACHE_TTL_SECONDS = 60


@dataclass(frozen=True)
class Region:
    code: str
    label: str
    city: str | None
    provider: str | None
    scheduled: bool
    voting: bool

    @property
    def display(self) -> str:
        return f"{self.label} ({self.city})" if self.city else self.label


_SQL = text(
    "SELECT code, label, city, provider, scheduled, voting FROM regions ORDER BY code"
)
_cache: tuple[float, tuple[Region, ...]] | None = None


def _rows_to_regions(rows: Iterable) -> tuple[Region, ...]:
    return tuple(Region(r[0], r[1], r[2], r[3], bool(r[4]), bool(r[5])) for r in rows)


def invalidate_cache() -> None:
    global _cache
    _cache = None


def _cached() -> tuple[Region, ...] | None:
    if _cache and time.monotonic() - _cache[0] < _CACHE_TTL_SECONDS:
        return _cache[1]
    return None


def _store(regions: tuple[Region, ...]) -> tuple[Region, ...]:
    global _cache
    _cache = (time.monotonic(), regions)
    return regions


def all_regions_sync(session) -> tuple[Region, ...]:
    """All regions (sync Session). Cached for 60 s per process."""
    hit = _cached()
    if hit is not None:
        return hit
    return _store(_rows_to_regions(session.execute(_SQL).fetchall()))


async def all_regions(db) -> tuple[Region, ...]:
    """All regions (AsyncSession). Cached for 60 s per process."""
    hit = _cached()
    if hit is not None:
        return hit
    return _store(_rows_to_regions((await db.execute(_SQL)).fetchall()))


def scheduled(regions: Iterable[Region]) -> list[Region]:
    return [r for r in regions if r.scheduled]


def voting(regions: Iterable[Region]) -> list[Region]:
    return [r for r in regions if r.voting]


def labels(regions: Iterable[Region]) -> dict[str, str]:
    """code -> 'US Central (Dallas)', for templates. Unknown codes fall back to the code."""
    return {r.code: r.display for r in regions}


def quorum(n_voting: int) -> int:
    """Votes needed for a verdict: strict majority (1->1, 2->2, 3->2, 4->3, 5->3)."""
    return n_voting // 2 + 1


def fresh_cutoff(interval_seconds: int | None, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    window = max(FRESH_INTERVALS * int(interval_seconds or 0), FRESH_MIN_SECONDS)
    return now - timedelta(seconds=window)


def consensus(latest: dict[str, str], voting_codes: Iterable[str]) -> tuple[str, int, int, int]:
    """Verdict from each voting region's latest fresh status ("up"/"down").

    ``latest`` maps region code -> status for regions with a fresh result;
    non-voting codes in it are ignored. Returns (verdict, down, reported, need)
    where verdict is "down", "up" or "incomplete" (no majority either way yet).
    """
    codes = list(voting_codes)
    need = quorum(len(codes))
    votes = [latest[c] for c in codes if c in latest]
    down = sum(1 for v in votes if v == "down")
    up = len(votes) - down
    if codes and down >= need:
        verdict = "down"
    elif codes and up >= need:
        verdict = "up"
    else:
        verdict = "incomplete"
    return verdict, down, len(votes), need
