"""Cache-behaviour tests for the source clients: a hit avoids the HTTP call, keys
include language, expiry refetches, --refresh bypasses, and a TVDB hit never triggers
login."""

from __future__ import annotations

from nalanda.cache import Cache
from nalanda.clients.mdblist import MDBListClient
from nalanda.clients.tmdb import TMDBClient
from nalanda.clients.tvdb import TVDBClient

YEAR = 30 * 86400


# --- TMDB ----------------------------------------------------------------------------


def _tmdb(cache, handler, *, language="en-US"):
    client = TMDBClient("testkey", language=language, cache=cache)
    calls: list[str] = []

    def fake_get(path, *, params=None, not_found_ok=False):
        calls.append(path)
        return handler(path, params or {})

    client.get = fake_get  # type: ignore[method-assign]
    return client, calls


def test_tmdb_record_hit_avoids_http(tmp_path):
    cache = Cache(tmp_path / "c.db", ttls={"tmdb.record": YEAR})
    c, calls = _tmdb(
        cache,
        lambda p, q: {"id": 550, "title": "Fight Club", "release_date": "1999-10-15"},
    )
    m1 = c.get_movie(550)
    assert m1.title == "Fight Club" and m1.tmdb_id == 550
    assert calls == ["3/movie/550"]
    m2 = c.get_movie(550)  # cache hit
    assert calls == ["3/movie/550"]  # no second HTTP call
    assert m2.tmdb_id == 550


def test_tmdb_record_keyed_by_language(tmp_path):
    cache = Cache(tmp_path / "c.db", ttls={"tmdb.record": YEAR})
    c_en, calls_en = _tmdb(
        cache, lambda p, q: {"id": 1, "title": "EN"}, language="en-US"
    )
    c_fr, calls_fr = _tmdb(
        cache, lambda p, q: {"id": 1, "title": "FR"}, language="fr-FR"
    )
    c_en.get_movie(1)
    c_fr.get_movie(1)  # different language -> different key -> separate fetch
    assert calls_en == ["3/movie/1"]
    assert calls_fr == ["3/movie/1"]


def test_tmdb_query_hit_avoids_http(tmp_path):
    cache = Cache(tmp_path / "c.db", ttls={"tmdb.query": 3 * 86400})
    c, calls = _tmdb(
        cache, lambda p, q: {"results": [{"id": 7, "title": "Q"}], "total_pages": 1}
    )
    r1 = c.discover_movies({"with_genres": "28"}, limit=10)
    assert [m.title for m in r1] == ["Q"]
    before = len(calls)
    r2 = c.discover_movies({"with_genres": "28"}, limit=10)  # hit
    assert len(calls) == before
    assert [m.tmdb_id for m in r2] == [7]


def test_tmdb_refresh_bypasses(tmp_path):
    p = tmp_path / "c.db"
    c1, _ = _tmdb(
        Cache(p, ttls={"tmdb.record": YEAR}), lambda pa, q: {"id": 1, "title": "X"}
    )
    c1.get_movie(1)  # populate
    c2, calls2 = _tmdb(
        Cache(p, ttls={"tmdb.record": YEAR}, refresh=True),
        lambda pa, q: {"id": 1, "title": "X"},
    )
    c2.get_movie(1)
    assert calls2 == ["3/movie/1"]  # refresh forced a fetch despite a fresh row


def test_tmdb_no_cache_still_works(tmp_path):
    c, calls = _tmdb(None, lambda p, q: {"id": 9, "title": "Z"})
    assert c.get_movie(9).tmdb_id == 9
    c.get_movie(9)
    assert calls == ["3/movie/9", "3/movie/9"]  # no cache -> always hits HTTP


# --- TVDB ----------------------------------------------------------------------------


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.ok = True
        self.content = b"{}"
        self.url = "http://tvdb/x"
        self.text = ""

    def json(self):
        return self._payload


def _tvdb_fake(calls):
    def fake_request(method, path, params=None, json=None):
        calls.append(path)
        if path == "login":
            return _Resp({"data": {"token": "TKN"}, "status": "success"})
        return _Resp(
            {"data": {"id": 5, "name": "Show", "year": "2010", "remoteIds": []}}
        )

    return fake_request


def test_tvdb_hit_does_not_log_in(tmp_path):
    cache = Cache(tmp_path / "c.db", ttls={"tvdb.record": YEAR})
    # client 1 populates the cache (and logs in once)
    c1 = TVDBClient("KEY", cache=cache)
    calls1: list[str] = []
    c1._session.request = _tvdb_fake(calls1)
    assert c1.get_series(5, extended=True).tvdb_id == 5
    assert "login" in calls1 and "series/5/extended" in calls1

    # client 2 is fresh (token is None); a cache hit must NOT log in or fetch
    c2 = TVDBClient("KEY", cache=cache)
    calls2: list[str] = []
    c2._session.request = _tvdb_fake(calls2)
    assert c2.get_series(5, extended=True).tvdb_id == 5
    assert calls2 == []  # no login, no data fetch


def test_tvdb_extended_flag_is_in_key(tmp_path):
    cache = Cache(tmp_path / "c.db", ttls={"tvdb.record": YEAR})
    c = TVDBClient("KEY", cache=cache)
    calls: list[str] = []
    c._session.request = _tvdb_fake(calls)
    c.get_series(5, extended=False)
    c.get_series(5, extended=True)  # different key -> separate fetch
    assert "series/5" in calls and "series/5/extended" in calls


# --- MDBList -------------------------------------------------------------------------


def _mdblist(cache, pages):
    client = MDBListClient("k", cache=cache)
    # Mark the supporter probe as already done so it doesn't consume a page slot.
    client._supporter_checked = True
    calls: list[int] = []

    def fake_get(path, *, params=None, not_found_ok=False):
        calls.append(1)
        return pages[len(calls) - 1]

    client.get = fake_get  # type: ignore[method-assign]
    return client, calls


def test_mdblist_list_hit_avoids_http(tmp_path):
    cache = Cache(tmp_path / "c.db", ttls={"mdblist.list": 86400})
    page = {"movies": [{"ids": {"tmdb": 11}}], "pagination": {"has_more": False}}
    c, calls = _mdblist(cache, [page, page])
    r1 = c.get_list("user/listname")
    assert [m.tmdb_id for m in r1] == [11]
    assert len(calls) == 1
    r2 = c.get_list("user/listname")  # hit
    assert len(calls) == 1  # no new HTTP
    assert [m.tmdb_id for m in r2] == [11]
