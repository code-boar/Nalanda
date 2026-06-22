"""Offline tests for the shared Servarr client base
(negative cache, bulk import, editor)."""

from __future__ import annotations

from nalanda.cache import ADD_FAILED_TTL, LOOKUP_EMPTY_TTL, LOOKUP_TTL, Cache
from nalanda.clients.radarr import RadarrClient

_RADARR_TTLS = {
    "radarr.lookup": LOOKUP_TTL,
    "radarr.lookup_empty": LOOKUP_EMPTY_TTL,
    "radarr.add_failed": ADD_FAILED_TTL,
}


def test_cached_lookup_negative_caches(tmp_path):
    cache = Cache(tmp_path / "c.db", ttls=_RADARR_TTLS)
    c = RadarrClient("http://r", "k", cache=cache)
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return None  # unknown id

    assert c._cached_lookup("tmdb:1", loader) is None
    assert c._cached_lookup("tmdb:1", loader) is None
    assert calls["n"] == 1  # second call served from the negative cache


def test_add_failed_roundtrip(tmp_path):
    cache = Cache(tmp_path / "c.db", ttls=_RADARR_TTLS)
    c = RadarrClient("http://r", "k", cache=cache)
    assert c._add_failed_recently("tmdb:5") is False
    c._mark_add_failed("tmdb:5")
    assert c._add_failed_recently("tmdb:5") is True
