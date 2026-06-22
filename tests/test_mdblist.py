"""Offline unit tests for the MDBList client (no network)."""

from __future__ import annotations

import pytest

from nalanda.clients.mdblist import MDBListClient, parse_list_url, parse_sort
from nalanda.models import MediaItem


# --- parse_list_url (tagged result: ("user",u,l) or ("external",id)) --------
def test_parse_user_listname():
    assert parse_list_url("https://mdblist.com/lists/someuser/some-list/") == (
        "user",
        "someuser",
        "some-list",
    )
    assert parse_list_url("http://mdblist.com/lists/u/l?x=1") == ("user", "u", "l")
    assert parse_list_url("someuser/test") == ("user", "someuser", "test")


def test_parse_external_url():
    assert parse_list_url("https://mdblist.com/lists/someuser/external/2868") == (
        "external",
        "2868",
    )


def test_parse_list_named_external_is_not_external():
    # a list literally named "external" (no numeric id) is a normal user list
    assert parse_list_url("someone/external") == ("user", "someone", "external")


def test_parse_invalid():
    with pytest.raises(ValueError):
        parse_list_url("not-a-list")


# --- parse_sort -----------------------------------------------------------------------
def test_parse_sort():
    assert parse_sort("imdbrating.desc") == ("imdbrating", "desc")
    assert parse_sort("rank.asc") == ("rank", "asc")
    assert parse_sort("score") == ("score", "desc")  # no suffix -> field, default desc
    assert parse_sort(None) == (None, "desc")


# --- client basics --------------------------------------------------------------------
def test_client_requires_key():
    with pytest.raises(ValueError):
        MDBListClient("")


def test_client_sets_apikey_param():
    assert MDBListClient("SECRET")._default_params.get("apikey") == "SECRET"


# --- MediaItem.from_mdblist (list shape + catalog shape + genre mapping) ------
def test_from_mdblist_list_item_with_append():
    item = {
        "id": 9010,
        "imdb_id": "tt9000004",
        "ids": {"tmdb": 9010, "imdb": "tt9000004"},
        "title": "Example Film",
        "release_year": 2026,
        "release_date": "2026-03-15",
        "description": "A short description for the test.",
        "genres": ["science-fiction", "drama", "unknown-slug"],
        "ratings": [{"source": "imdb", "value": 8.3}],
    }
    resolver = {"science-fiction": 878, "drama": 18}.get
    movie = MediaItem.from_mdblist(item, genre_resolver=resolver)
    assert movie.tmdb_id == 9010
    assert movie.release_date == "2026-03-15"
    assert movie.overview.startswith("A short")
    assert movie.genre_ids == [878, 18]  # unknown slug dropped
    assert movie.ratings == [{"source": "imdb", "value": 8.3}]


def test_from_mdblist_catalog_shape():
    # catalog items use ids.tmdbid / year (no top-level id, no genres)
    item = {"title": "X", "year": 2020, "ids": {"tmdbid": 99, "imdbid": "tt9"}}
    movie = MediaItem.from_mdblist(item)
    assert movie.tmdb_id == 99
    assert movie.imdb_id == "tt9"
    assert movie.year == 2020


def test_from_mdblist_zero_id_coerced_to_none():
    movie = MediaItem.from_mdblist(
        {"id": 0, "imdb_id": "tt9", "title": "X", "release_year": 2000}
    )
    assert movie.tmdb_id is None
    assert movie.imdb_id == "tt9"


# --- pagination / response handling (monkeypatched .get) ---------------------
def _client_with_pages(monkeypatch, pages):
    client = MDBListClient("k")
    # Mark the supporter probe as already done so it doesn't consume a page slot.
    client._supporter_checked = True
    calls = []

    def fake_get(path, *, params=None, not_found_ok=False):
        calls.append((params or {}).get("cursor"))
        return pages[len(calls) - 1]

    monkeypatch.setattr(client, "get", fake_get)
    return client, calls


def test_cursor_pagination_follows_next_cursor(monkeypatch):
    pages = [
        {
            "movies": [{"ids": {"tmdb": 1}}, {"ids": {"tmdb": 2}}],
            "pagination": {"has_more": True, "next_cursor": "C1"},
        },
        {
            "movies": [{"ids": {"tmdb": 3}}],
            "pagination": {"has_more": False, "next_cursor": None},
        },
    ]
    client, calls = _client_with_pages(monkeypatch, pages)
    movies = client.get_list("u/l")
    assert [m.tmdb_id for m in movies] == [1, 2, 3]
    assert calls == [None, "C1"]  # first page no cursor, then the cursor from page 1


def test_limit_truncates_and_stops_paging(monkeypatch):
    pages = [
        {
            "movies": [{"ids": {"tmdb": i}} for i in range(5)],
            "pagination": {"has_more": True, "next_cursor": "X"},
        }
    ]
    client, _ = _client_with_pages(monkeypatch, pages)
    assert (
        len(client.get_list("u/l", limit=3)) == 3
    )  # stops mid-page, doesn't fetch page 2


def test_error_response_raises(monkeypatch):
    client, _ = _client_with_pages(monkeypatch, [{"error": "This List is Private"}])
    with pytest.raises(ValueError):
        client.get_list("u/l")


def test_get_catalog_and_official(monkeypatch):
    pages = [
        {
            "movies": [{"title": "X", "year": 2020, "ids": {"tmdbid": 99}}],
            "pagination": {"has_more": False},
        }
    ]
    client, _ = _client_with_pages(monkeypatch, pages)
    assert client.get_catalog({"sort": "rtomatoes"})[0].tmdb_id == 99
    client2, _ = _client_with_pages(monkeypatch, pages)
    assert client2.get_official_movies("popular")[0].tmdb_id == 99


def test_from_mdblist_show_captures_tvdb_and_media_type():
    item = {
        "ids": {"tmdb": 9002, "tvdb": 9001, "imdb": "tt9000001"},
        "title": "Example Show",
    }
    show = MediaItem.from_mdblist(item, media_type="tv")
    assert show.media_type == "tv"
    assert show.tvdb_id == 9001 and show.tmdb_id == 9002 and show.imdb_id == "tt9000001"


def test_media_tv_reads_shows_array_not_movies(monkeypatch):
    # one payload carries BOTH arrays; a tv collection must take `shows` (with tvdb ids)
    pages = [
        {
            "movies": [{"ids": {"tmdb": 1}}],
            "shows": [
                {"ids": {"tmdb": 9002, "tvdb": 9001}},
                {"ids": {"tmdb": 1399, "tvdb": 121361}},
            ],
            "pagination": {"has_more": False},
        }
    ]
    client, _ = _client_with_pages(monkeypatch, pages)
    shows = client.get_list("u/l", media="tv")
    assert [s.tvdb_id for s in shows] == [9001, 121361]
    assert all(s.media_type == "tv" for s in shows)


def test_catalog_show_hits_show_endpoint(monkeypatch):
    seen_paths = []
    client = MDBListClient("k")
    # Mark the supporter probe as already done so it doesn't appear in seen_paths.
    client._supporter_checked = True

    def fake_get(path, *, params=None, not_found_ok=False):
        seen_paths.append(path)
        return {
            "shows": [{"ids": {"tmdb": 5, "tvdb": 50}}],
            "pagination": {"has_more": False},
        }

    monkeypatch.setattr(client, "get", fake_get)
    out = client.get_catalog({"sort": "rtomatoes"}, media="tv")
    assert seen_paths == ["catalog/show"]
    assert out[0].tvdb_id == 50
