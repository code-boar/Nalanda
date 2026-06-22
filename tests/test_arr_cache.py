"""Arr add-lookup cache (incl. negative cache), bulk-import batching, and the failed-add
marker -- all offline with a real Cache and shadowed get/post."""

from __future__ import annotations

import pytest

from nalanda.cache import ADD_FAILED_TTL, LOOKUP_EMPTY_TTL, LOOKUP_TTL, Cache
from nalanda.clients.radarr import RadarrClient
from nalanda.clients.sonarr import SonarrClient
from nalanda.http import HTTPError

# --- Radarr ------------------------------------------------------------------


def _radarr(tmp_path):
    cache = Cache(
        tmp_path / "c.db",
        ttls={
            "radarr.lookup": LOOKUP_TTL,
            "radarr.lookup_empty": LOOKUP_EMPTY_TTL,
            "radarr.add_failed": ADD_FAILED_TTL,
        },
    )
    return RadarrClient("http://h:7878", "k", cache=cache)


def test_radarr_lookup_cache_avoids_repeat_upstream(tmp_path):
    c = _radarr(tmp_path)
    gets: list[str] = []
    c.get = lambda path, **kw: (
        gets.append(path)
        or {"tmdbId": 5, "title": "X", "titleSlug": "x-5", "year": 2000, "id": 0}
    )
    assert c.build_add_payload(5, quality_profile_id=1, root_folder="/m")["tmdbId"] == 5
    assert len(gets) == 1
    assert c.build_add_payload(5, quality_profile_id=1, root_folder="/m")["tmdbId"] == 5
    assert len(gets) == 1  # cache hit -> no second upstream lookup


def test_radarr_negative_cache(tmp_path):
    c = _radarr(tmp_path)
    gets: list[str] = []
    c.get = lambda path, **kw: gets.append(path) or None  # unknown id -> empty lookup
    assert c.build_add_payload(99, quality_profile_id=1, root_folder="/m") is None
    assert c.build_add_payload(99, quality_profile_id=1, root_folder="/m") is None
    assert len(gets) == 1  # empty lookup negative-cached -> no second upstream call


def test_radarr_add_movies_batches_import():
    c = RadarrClient("http://h:7878", "k")
    posts: list[tuple[str, int]] = []

    def fake_post(path, **kw):
        body = kw["json"]
        posts.append((path, len(body)))
        return [
            {"id": i, "tmdbId": p["tmdbId"], "title": "x"} for i, p in enumerate(body)
        ]

    c.post = fake_post
    added = c.add_movies([{"tmdbId": i} for i in range(250)])
    assert [n for _, n in posts] == [100, 100, 50]
    assert all(path.endswith("/movie/import") for path, _ in posts)
    assert len(added) == 250


def test_radarr_failed_add_marker(tmp_path):
    c = _radarr(tmp_path)
    assert c.add_failed_recently(7) is False
    c.mark_add_failed(7)
    assert c.add_failed_recently(7) is True


def test_radarr_no_cache_marker_is_noop():
    c = RadarrClient("http://h:7878", "k")  # no cache
    c.mark_add_failed(7)  # no-op, must not raise
    assert c.add_failed_recently(7) is False


# --- Sonarr ------------------------------------------------------------------


def _sonarr(tmp_path):
    cache = Cache(
        tmp_path / "c.db",
        ttls={
            "sonarr.lookup": LOOKUP_TTL,
            "sonarr.lookup_empty": LOOKUP_EMPTY_TTL,
            "sonarr.add_failed": ADD_FAILED_TTL,
        },
    )
    return SonarrClient("http://h:8989", "k", cache=cache)


def test_sonarr_lookup_cache_avoids_repeat_upstream(tmp_path):
    c = _sonarr(tmp_path)
    gets: list[str] = []
    c.get = lambda path, **kw: (
        gets.append(path) or [{"tvdbId": 1, "title": "X", "titleSlug": "x"}]
    )
    p1 = c.build_add_payload("tvdb:1", quality_profile_id=1, root_folder="/tv")
    assert p1["tvdbId"] == 1 and len(gets) == 1
    p2 = c.build_add_payload("tvdb:1", quality_profile_id=1, root_folder="/tv")
    assert p2["tvdbId"] == 1 and len(gets) == 1  # cache hit


def test_sonarr_negative_cache(tmp_path):
    c = _sonarr(tmp_path)
    gets: list[str] = []
    c.get = lambda path, **kw: gets.append(path) or []  # empty lookup
    assert (
        c.build_add_payload("tvdb:999", quality_profile_id=1, root_folder="/tv") is None
    )
    assert (
        c.build_add_payload("tvdb:999", quality_profile_id=1, root_folder="/tv") is None
    )
    assert len(gets) == 1


def test_sonarr_add_series_bulk_batches_import():
    c = SonarrClient("http://h:8989", "k")
    posts: list[tuple[str, int]] = []

    def fake_post(path, **kw):
        body = kw["json"]
        posts.append((path, len(body)))
        return [
            {"id": i, "tvdbId": p["tvdbId"], "title": "x"} for i, p in enumerate(body)
        ]

    c.post = fake_post
    added = c.add_series_bulk([{"tvdbId": i} for i in range(250)])
    assert [n for _, n in posts] == [100, 100, 50]
    assert all(path.endswith("/series/import") for path, _ in posts)
    assert len(added) == 250


def test_sonarr_failed_add_marker(tmp_path):
    c = _sonarr(tmp_path)
    assert c.add_failed_recently("tvdb:1") is False
    c.mark_add_failed("tvdb:1")
    assert c.add_failed_recently("tvdb:1") is True


# --- bulk-import 400 fallback (verified live: /import is all-or-nothing) -----


def test_radarr_add_movies_falls_back_to_per_item_on_400():
    c = RadarrClient("http://h:7878", "k")
    calls: list[str] = []

    def fake_post(path, **kw):
        calls.append(path)
        if path.endswith("/movie/import"):
            raise HTTPError("batch rejected", status=400, body="[errors]")
        body = kw["json"]
        if body["tmdbId"] == 2:
            raise HTTPError("already exists", status=400)  # one invalid item, isolated
        return {"id": 10, "tmdbId": body["tmdbId"], "title": "x"}

    c.post = fake_post
    added = c.add_movies([{"tmdbId": 1}, {"tmdbId": 2}, {"tmdbId": 3}])
    assert sorted(a.tmdb_id for a in added) == [1, 3]  # valid items still added
    assert calls[0].endswith("/movie/import")  # bulk tried first
    assert (
        sum(1 for p in calls if p.endswith("/movie")) == 3
    )  # then per-item for the chunk


def test_radarr_add_movies_propagates_non_400():
    c = RadarrClient("http://h:7878", "k")

    def boom(path, **kw):
        raise HTTPError("server error", status=500)

    c.post = boom
    with pytest.raises(HTTPError):
        c.add_movies([{"tmdbId": 1}])


def test_sonarr_add_series_bulk_falls_back_on_400():
    c = SonarrClient("http://h:8989", "k")
    calls: list[str] = []

    def fake_post(path, **kw):
        calls.append(path)
        if path.endswith("/series/import"):
            raise HTTPError("batch rejected", status=400)
        body = kw["json"]
        if body["tvdbId"] == 2:
            raise HTTPError("exists", status=400)
        return {"id": 10, "tvdbId": body["tvdbId"], "title": "x"}

    c.post = fake_post
    added = c.add_series_bulk([{"tvdbId": 1}, {"tvdbId": 2}])
    assert [s.tvdb_id for s in added] == [1]
    assert calls[0].endswith("/series/import")
    assert sum(1 for p in calls if p.endswith("/series")) == 2
