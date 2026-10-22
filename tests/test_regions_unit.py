"""CP-1: the verdict rule, as pure functions (no DB)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.services import regions as r


@pytest.mark.parametrize("n,need", [(1, 1), (2, 2), (3, 2), (4, 3), (5, 3), (6, 4), (7, 4)])
def test_quorum_is_strict_majority(n, need):
    assert r.quorum(n) == need


V3 = ["us-east", "us-central", "us-west"]


@pytest.mark.parametrize("latest,verdict", [
    ({"us-east": "up", "us-central": "up", "us-west": "up"}, "up"),
    ({"us-east": "down", "us-central": "up", "us-west": "up"}, "up"),          # one bad region: no alert
    ({"us-east": "down", "us-central": "down", "us-west": "up"}, "down"),      # 2 of 3
    ({"us-east": "down", "us-central": "down", "us-west": "down"}, "down"),
    ({"us-east": "down", "us-central": "down"}, "down"),                       # majority already reached
    ({"us-east": "up", "us-central": "up"}, "up"),
    ({"us-east": "up", "us-central": "down"}, "incomplete"),                   # 1-1, third missing
    ({"us-east": "down"}, "incomplete"),
    ({}, "incomplete"),
])
def test_consensus_three_voters(latest, verdict):
    assert r.consensus(latest, V3)[0] == verdict


def test_non_voting_results_never_count():
    """A shadow region reporting DOWN cannot tip the verdict."""
    latest = {"us-east": "up", "us-central": "down", "shadow-1": "down", "shadow-2": "down"}
    verdict, down, reported, need = r.consensus(latest, ["us-east", "us-central", "us-west"])
    assert (verdict, down, reported, need) == ("incomplete", 1, 2, 2)


def test_five_voters_need_three():
    v5 = ["a1", "a2", "a3", "a4", "a5"]
    assert r.consensus({"a1": "down", "a2": "down", "a3": "up", "a4": "up", "a5": "up"}, v5)[0] == "up"
    assert r.consensus({"a1": "down", "a2": "down", "a3": "down", "a4": "up", "a5": "up"}, v5)[0] == "down"


def test_no_voting_regions_is_incomplete_not_up():
    assert r.consensus({"us": "up"}, [])[0] == "incomplete"


def test_fresh_cutoff_floor_and_multiple():
    now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    assert r.fresh_cutoff(60, now) == now - timedelta(minutes=15)        # floor wins
    assert r.fresh_cutoff(300, now) == now - timedelta(minutes=15)       # 3x300s = 15 min
    assert r.fresh_cutoff(3600, now) == now - timedelta(hours=3)         # 3x interval wins
    assert r.fresh_cutoff(None, now) == now - timedelta(minutes=15)


def test_region_display():
    assert r.Region("us-west", "US West", "San Francisco", "do", True, True).display == "US West (San Francisco)"
    assert r.Region("x", "X", None, None, True, False).display == "X"
