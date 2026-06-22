"""Offline tests for `media: mixed` block resolution (shared keys produce BOTH media).

These exercise ``_resolve_block(..., media="mixed")`` directly: media-specific keys
contribute only their own media, shared keys contribute movies AND shows, and the two
are merged chronologically.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nalanda.builders import _resolve_block, run_builders
from nalanda.config import BuilderBlock, CollectionDef
from nalanda.models import MediaItem, MovieCollection


class _FakeMixed:
    """A TMDB stub exposing both the movie and TV methods a mixed block
    dispatches to."""

    def get_collection(self, cid):  # movie-only key
        return MovieCollection(
            tmdb_id=cid,
            name=f"C{cid}",
            movies=[
                MediaItem(title="film-a", tmdb_id=10, release_date="2015-01-01"),
                MediaItem(title="film-b", tmdb_id=11, release_date="2019-01-01"),
            ],
        )

    def get_show(self, sid):  # tv-only key
        return MediaItem(
            title=f"show-{sid}",
            tmdb_id=sid,
            tvdb_id=sid * 100,
            media_type="tv",
            release_date="2017-01-01",
        )

    def resolve_genre(self, name, *, media="movie"):  # shared key -- distinct id spaces
        ids = {"tv": {"action": 10759}, "movie": {"action": 28}}
        return ids[media].get(name.casefold())

    def get_genre_movies(self, expr, *, limit=None):
        assert expr == "28"  # the movie genre id, not the tv one
        return [MediaItem(title="gmovie", tmdb_id=20, release_date="2016-01-01")]

    def get_genre_shows(self, expr, *, limit=None):
        assert expr == "10759"  # the tv genre id
        return [
            MediaItem(
                title="gshow", tmdb_id=21, media_type="tv", release_date="2018-01-01"
            )
        ]


def _resolve(block, **kw):
    return _resolve_block(
        "X", block, tmdb=_FakeMixed(), mdblist=None, media="mixed", **kw
    )[0]


def test_mixed_combines_media_specific_keys_chronologically():
    # A franchise: the films (movie collection) + the series (tv show), interleaved
    # by date.
    items = _resolve(BuilderBlock(tmdb_collection=1, tmdb_show=[700]))
    assert [(m.title, m.media_type) for m in items] == [
        ("film-a", "movie"),  # 2015
        ("show-700", "tv"),  # 2017
        ("film-b", "movie"),  # 2019
    ]


def test_mixed_shared_key_produces_both_media():
    # A shared key (genre) resolves once per media -- distinct genre ids -- and
    # yields both.
    items = _resolve(BuilderBlock(tmdb_genre="Action"))
    assert {(m.title, m.media_type) for m in items} == {
        ("gmovie", "movie"),
        ("gshow", "tv"),
    }


def test_mixed_movie_only_key_contributes_only_movies():
    # The tv pass masks off movie-only keys, so tmdb_collection yields nothing
    # as a show.
    items = _resolve(BuilderBlock(tmdb_collection=1))
    assert {m.media_type for m in items} == {"movie"}


def test_mixed_tv_only_key_contributes_only_shows():
    items = _resolve(BuilderBlock(tmdb_show=[700]))
    assert {m.media_type for m in items} == {"tv"}


def test_mixed_run_builders_end_to_end():
    # Through run_builders (which requires media="mixed" to be a valid schema value).
    coll = CollectionDef(media="mixed", tmdb_collection=1, tmdb_show=[700])
    result = run_builders("A Mixed Collection", coll, tmdb=_FakeMixed(), mdblist=None)
    assert {m.media_type for m in result.movies} == {"movie", "tv"}
    assert {m.title for m in result.movies} == {"film-a", "film-b", "show-700"}


def test_mixed_allows_both_arr_blocks_and_key_families():
    # A mixed collection may carry both radarr and sonarr, and both movie- and
    # tv-only keys.
    # `enable` defaults to false, so a bare block is inert until explicitly opted in.
    coll = CollectionDef(
        media="mixed",
        tmdb_collection=1,
        tmdb_show=[700],
        radarr={},
        sonarr={"enable": True},
    )
    assert coll.radarr is not None and coll.radarr.enable is False  # bare block -> off
    assert coll.sonarr is not None and coll.sonarr.enable is True  # opted in


def test_non_mixed_still_rejects_cross_media_arr_blocks():
    with pytest.raises(ValidationError):
        CollectionDef(media="movie", tmdb_collection=1, sonarr={})
    with pytest.raises(ValidationError):
        CollectionDef(media="tv", tmdb_show=[1], radarr={})


def test_mixed_build_then_match_routes_each_member_to_its_media():
    # End-to-end-ish: build a mixed set, then match it via the media-routing index.
    # The movie member resolves against the movie library, the show against the show
    # library -- and the show's tmdb id is never looked up in the movie index
    # (no cross-namespace match).
    from nalanda.matching import LibraryIndex, MediaRoutedIndex
    from nalanda.models import JellyfinItem

    built = run_builders(
        "SW",
        CollectionDef(media="mixed", tmdb_collection=1, tmdb_show=[700]),
        tmdb=_FakeMixed(),
        mdblist=None,
    )
    routed = MediaRoutedIndex(
        {
            "movie": LibraryIndex(
                [
                    JellyfinItem(
                        id="jf-film-a", name="film-a", provider_ids={"Tmdb": "10"}
                    )
                ]
            ),
            "tv": LibraryIndex(
                [
                    JellyfinItem(
                        id="jf-show", name="show-700", provider_ids={"Tmdb": "700"}
                    )
                ]
            ),
        }
    )
    hits = {m.title: (h.id if (h := routed.find(m)) else None) for m in built.movies}
    assert hits == {"film-a": "jf-film-a", "film-b": None, "show-700": "jf-show"}
