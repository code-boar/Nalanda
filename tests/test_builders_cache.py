"""End-to-end cache test at the builder seam: a second run over the same collection
makes zero source HTTP calls and resolves to the identical set -- the cache's headline
guarantee.

Uses a fresh client for the second pass (empty in-memory caches) sharing the warm
on-disk cache, so it exercises the SQLite cache across client lifetimes, the way two
real runs would.
"""

from __future__ import annotations

from nalanda.builders import run_builders
from nalanda.cache import Cache, ttl_map
from nalanda.clients.tmdb import TMDBClient
from nalanda.config import CacheSettings, CollectionDef


def _fake_tmdb_get(calls: list[str]):
    """A canned TMDB transport covering the endpoints the collection below touches."""

    def get(path, *, params=None, not_found_ok=False):
        calls.append(path)
        if path.startswith("3/collection/"):
            return {
                "id": 10,
                "name": "Test Collection",
                "overview": "overview",
                "parts": [
                    {"id": 1, "title": "Part A", "release_date": "2001-01-01"},
                    {"id": 2, "title": "Part B", "release_date": "2002-01-01"},
                ],
                "poster_path": "/p.jpg",
                "images": {"backdrops": []},
            }
        if path.startswith("3/movie/"):
            return {"id": 600, "title": "A Film Title", "release_date": "1999-01-01"}
        if path == "3/genre/movie/list":
            return {"genres": [{"id": 28, "name": "Action"}]}
        if path == "3/discover/movie":
            return {
                "results": [
                    {"id": 100, "title": "Discovered", "release_date": "2020-01-01"}
                ],
                "total_pages": 1,
            }
        raise AssertionError(f"unexpected TMDB path requested: {path}")

    return get


def test_second_run_builders_hits_cache_not_network(tmp_path):
    cache = Cache(tmp_path / "cache.db", ttls=ttl_map(CacheSettings()))
    coll = CollectionDef(
        media="movie",
        tmdb_collection=10,  # tmdb.record (get_collection)
        tmdb_movie=[600],  # tmdb.record (get_movie)
        tmdb_genre="action",  # tmdb.resolve (genre map) + tmdb.query (discover)
    )

    # First run populates the cache and must hit the network.
    calls1: list[str] = []
    c1 = TMDBClient("apikey", cache=cache)
    c1.get = _fake_tmdb_get(calls1)
    first = run_builders("Test", coll, tmdb=c1, mdblist=None, tvdb=None)
    assert first.movies  # resolved something
    assert calls1, "first run should hit the network"

    # Second run: a fresh client (empty in-memory genre cache) sharing the warm
    # disk cache.
    calls2: list[str] = []
    c2 = TMDBClient("apikey", cache=cache)
    c2.get = _fake_tmdb_get(calls2)
    second = run_builders("Test", coll, tmdb=c2, mdblist=None, tvdb=None)

    assert calls2 == []  # zero source HTTP calls -- every builder served from the cache
    assert (
        second.movies == first.movies
    )  # identical resolved set, round-tripped cleanly


def test_run_builders_uncached_repeats_network(tmp_path):
    # Sanity check the test's own premise: with no cache, the second run DOES hit
    # the network.
    coll = CollectionDef(media="movie", tmdb_collection=10)
    calls1: list[str] = []
    c1 = TMDBClient("apikey")  # no cache
    c1.get = _fake_tmdb_get(calls1)
    run_builders("Test", coll, tmdb=c1, mdblist=None, tvdb=None)

    calls2: list[str] = []
    c2 = TMDBClient("apikey")
    c2.get = _fake_tmdb_get(calls2)
    run_builders("Test", coll, tmdb=c2, mdblist=None, tvdb=None)
    assert calls2, "without a cache the second run must re-fetch"
