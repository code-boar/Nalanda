"""Offline tests for the TV (media: tv) builder paths and the media validator."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nalanda.builders import run_builders
from nalanda.config import CollectionDef
from nalanda.models import MediaItem


def _show(title, tmdb, *, tvdb=None, date="2010-01-01"):
    return MediaItem(
        title=title, tmdb_id=tmdb, tvdb_id=tvdb, media_type="tv", release_date=date
    )


class _FakeTV:
    """A TMDB client stub exposing only the TV methods the builder dispatches to."""

    def __init__(self):
        self.calls: list = []

    def get_show(self, sid):
        self.calls.append(("get_show", sid))
        return _show(f"S{sid}", sid, tvdb=sid * 100)

    def find_show_by_title(self, title, *, year=None):
        self.calls.append(("find_show", title, year))
        return _show(title, 42, date="2015-01-01")

    def resolve_genre(self, name, *, media="movie"):
        assert media == "tv"  # the builder must ask for tv genres
        return {"drama": 18, "comedy": 35}.get(name.casefold())

    def get_genre_shows(self, expr, *, limit=None):
        self.calls.append(("genre_shows", expr, limit))
        return [_show("g1", 1, date="2001-01-01"), _show("g2", 2, date="2003-01-01")]

    def get_keyword_shows(self, expr, *, without_keywords=None, limit=None):
        self.calls.append(("keyword_shows", expr, without_keywords))
        return [_show("k", 5, date="2008-01-01")]

    def search_keyword(self, name):
        return 55

    def get_network_shows(self, nid, *, limit=None):
        self.calls.append(("network", nid, limit))
        return [_show(f"n{nid}", nid, date="2018-01-01")]

    def get_tv_chart(self, chart, count):
        self.calls.append(("tv_chart", chart, count))
        return [
            _show(f"{chart}{i}", 200 + i, date=f"20{i:02d}-01-01") for i in range(count)
        ]

    def discover_shows(self, filters, *, limit=None):
        self.calls.append(("discover_shows", dict(filters), limit))
        return [_show("d", 7, date="2012-01-01")]


def test_from_tmdb_tv_parses_name_date_and_external_ids():
    item = MediaItem.from_tmdb_tv(
        {
            "id": 9002,
            "name": "Example Show",
            "first_air_date": "2008-01-20",
            "overview": "A chemistry teacher...",
            "genre_ids": [18],
            "external_ids": {"tvdb_id": 9001, "imdb_id": "tt9000001"},
        }
    )
    assert item.media_type == "tv"
    assert item.title == "Example Show"
    assert item.year == 2008 and item.release_date == "2008-01-20"
    assert item.tmdb_id == 9002 and item.tvdb_id == 9001 and item.imdb_id == "tt9000001"


def test_tmdb_show_resolves_via_get_show():
    fake = _FakeTV()
    coll = CollectionDef(media="tv", tmdb_show=[9002, 1399])
    result = run_builders("X", coll, tmdb=fake, mdblist=None)
    assert {m.tmdb_id for m in result.movies} == {9002, 1399}
    assert all(m.media_type == "tv" and m.tvdb_id for m in result.movies)
    assert ("get_show", 9002) in fake.calls


def test_tv_popular_chart_uses_tv_endpoint():
    fake = _FakeTV()
    coll = CollectionDef(media="tv", tmdb_popular=3)
    result = run_builders("X", coll, tmdb=fake, mdblist=None)
    assert ("tv_chart", "popular", 3) in fake.calls
    assert len(result.movies) == 3
    assert result.release_sorted is False  # sole curated chart keeps server order


def test_on_the_air_is_tv_chart():
    fake = _FakeTV()
    coll = CollectionDef(media="tv", tmdb_on_the_air=2)
    run_builders("X", coll, tmdb=fake, mdblist=None)
    assert ("tv_chart", "on_the_air", 2) in fake.calls


def test_genre_dispatches_to_tv_discover():
    fake = _FakeTV()
    coll = CollectionDef(media="tv", tmdb_genre={"any": ["Drama", "Comedy"]})
    run_builders("X", coll, tmdb=fake, mdblist=None)
    assert (
        "genre_shows",
        "18|35",
        100,
    ) in fake.calls  # tv genre ids, OR-joined, default limit


def test_network_accepts_ids_and_rejects_names():
    fake = _FakeTV()
    run_builders(
        "X", CollectionDef(media="tv", tmdb_network=9008), tmdb=fake, mdblist=None
    )
    assert ("network", 9008, 100) in fake.calls
    with pytest.raises(ValueError):  # networks have no name search
        run_builders(
            "X",
            CollectionDef(media="tv", tmdb_network="A Network"),
            tmdb=fake,
            mdblist=None,
        )


def test_discover_tv_dispatch():
    fake = _FakeTV()
    coll = CollectionDef(media="tv", tmdb_discover={"with_status": "0", "limit": 10})
    run_builders("X", coll, tmdb=fake, mdblist=None)
    assert fake.calls and fake.calls[0][0] == "discover_shows"
    assert fake.calls[0][1] == {"with_status": "0"}  # limit stripped out


# --- media validator -------------------------------------------------------


def test_validator_rejects_movie_only_key_on_tv():
    with pytest.raises(ValidationError):
        CollectionDef(media="tv", tmdb_movie=1)
    with pytest.raises(ValidationError):
        CollectionDef(media="tv", tmdb_collection=119)
    with pytest.raises(ValidationError):
        CollectionDef(media="tv", tmdb_now_playing=5)


def test_validator_rejects_tv_only_key_on_movie():
    with pytest.raises(ValidationError):
        CollectionDef(media="movie", tmdb_show=1)
    with pytest.raises(ValidationError):
        CollectionDef(media="movie", tmdb_on_the_air=5)
    with pytest.raises(ValidationError):
        CollectionDef(media="movie", tmdb_network=213)


def test_validator_checks_append_blocks_too():
    # a TV-only key hidden in an append block is still rejected on a movie collection
    with pytest.raises(ValidationError):
        CollectionDef(media="movie", tmdb_movie=1, append=[{"tmdb_show": 99}])


# --- TVDB builders ---------------------------------------------------------


class _FakeTVDB:
    def __init__(self):
        self.calls: list = []

    def resolve(self, value, *, media):
        self.calls.append(("resolve", value, media))
        if media == "tv":
            return _show(
                f"tvdb-{value}", 1000, tvdb=int(value) if str(value).isdigit() else 1
            )
        return MediaItem(
            title=f"m-{value}",
            tmdb_id=603,
            tvdb_id=100,
            media_type="movie",
            release_date="2019-01-01",
        )

    def get_list(self, ref, *, media):
        self.calls.append(("get_list", ref, media))
        return [
            _show("L1", 1, tvdb=11, date="2001-01-01"),
            _show("L2", 2, tvdb=12, date="2002-01-01"),
        ]

    def discover(self, filters, *, media, limit=None):
        self.calls.append(("discover", dict(filters), media, limit))
        return [_show("d", 7, tvdb=70, date="2012-01-01")]


def test_tvdb_show_resolves_as_tv():
    tvdb = _FakeTVDB()
    coll = CollectionDef(media="tv", tvdb_show=[9001, 121361])
    result = run_builders("X", coll, tmdb=None, mdblist=None, tvdb=tvdb)
    assert ("resolve", 9001, "tv") in tvdb.calls
    assert all(m.media_type == "tv" for m in result.movies)


def test_tvdb_movie_resolves_as_movie_with_tmdb():
    tvdb = _FakeTVDB()
    coll = CollectionDef(media="movie", tvdb_movie=12345)
    result = run_builders("X", coll, tmdb=None, mdblist=None, tvdb=tvdb)
    assert ("resolve", 12345, "movie") in tvdb.calls
    assert result.movies[0].tmdb_id == 603  # enriched for Radarr/Jellyfin


def test_tvdb_list_and_discover_dispatch_by_media():
    tvdb = _FakeTVDB()
    run_builders(
        "X",
        CollectionDef(media="tv", tvdb_list="my-list"),
        tmdb=None,
        mdblist=None,
        tvdb=tvdb,
    )
    run_builders(
        "X",
        CollectionDef(media="tv", tvdb_discover={"genre": 3, "limit": 5}),
        tmdb=None,
        mdblist=None,
        tvdb=tvdb,
    )
    assert ("get_list", "my-list", "tv") in tvdb.calls
    assert (
        "discover",
        {"genre": 3},
        "tv",
        5,
    ) in tvdb.calls  # limit stripped + forwarded


def test_tvdb_builder_without_client_errors():
    with pytest.raises(ValueError):
        run_builders(
            "X",
            CollectionDef(media="tv", tvdb_show=1),
            tmdb=None,
            mdblist=None,
            tvdb=None,
        )


def test_validator_tvdb_media_gating():
    with pytest.raises(ValidationError):
        CollectionDef(media="movie", tvdb_show=1)  # tv-only
    with pytest.raises(ValidationError):
        CollectionDef(media="tv", tvdb_movie=1)  # movie-only


def test_validator_tmdb_overview_media_gating():
    # tmdb_overview pulls a movie-collection's overview text: valid on movie + mixed
    # (the text is generic), rejected only on a pure-tv collection.
    with pytest.raises(ValidationError):
        CollectionDef(media="tv", tmdb_show=[1], tmdb_overview=10)
    CollectionDef(media="movie", tmdb_collection=10, tmdb_overview=10)  # ok
    CollectionDef(media="mixed", tmdb_collection=10, tmdb_overview=10)  # ok
