"""Offline tests for the TVDB v4 client (login flow + record parsing), no network.

The fake replaces ``session.request`` so the real auth + request_json stack runs,
including the lazy ``/login`` on the first call.
"""

from __future__ import annotations

import nalanda.clients.tvdb as tvdb_mod
from nalanda.clients.tvdb import TVDBClient, _strand, _unstrand, bundled_tvdb_key


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.ok = 200 <= status < 300
        self.content = b"{}" if payload is not None else b""
        self.url = "http://tvdb/x"
        self.text = ""

    def json(self):
        return self._payload


def _client(handler, *, api_key="KEY", pin=None):
    client = TVDBClient(api_key, pin)
    calls: list = []

    def fake_request(method, path, params=None, json=None):
        calls.append((method, path, params, json))
        if path == "login":
            return _Resp({"data": {"token": "TKN"}, "status": "success"})
        return _Resp(handler(method, path, params or {}, json))

    client._session.request = fake_request
    return client, calls


def test_constructs_without_a_key():
    # No user key needed -- the bundled project key is used. Construction must
    # not require one and must not log in (lazy).
    client = TVDBClient()
    assert client._token is None


# --- bundled-key obfuscation ----------------------------------------------


def test_strand_round_trips():
    key = "12345678-aaaa-bbbb-cccc-0123456789ab"  # uuid-ish, like a TVDB v4 key
    k1, k2 = _strand(key)
    # strands are interleaved (k1 = even chars, k2 = odd) -> neither is the key,
    # nor a base64 blob that decodes to it on its own.
    assert key not in k1 and key not in k2
    assert _unstrand(k1, k2) == key


def test_bundled_key_used_when_no_explicit_key(monkeypatch):
    # set known strands, then a keyless client must log in with the decoded bundled key
    k1, k2 = _strand("real-project-key")
    monkeypatch.setattr(tvdb_mod, "_K1", k1)
    monkeypatch.setattr(tvdb_mod, "_K2", k2)
    assert bundled_tvdb_key() == "real-project-key"
    client = TVDBClient()  # no explicit key
    calls: list = []

    def fake_request(method, path, params=None, json=None):
        calls.append((method, path, json))
        if path == "login":
            return _Resp({"data": {"token": "TKN"}})
        return _Resp({"data": {"id": 1, "name": "S"}})

    client._session.request = fake_request
    client.get_series(1)
    assert calls[0] == (
        "POST",
        "login",
        {"apikey": "real-project-key"},
    )  # bundled key, no pin


def test_lazy_login_sets_bearer_once_and_sends_pin():
    client, calls = _client(
        lambda m, p, par, j: {"data": {"id": 1, "name": "S", "year": "2010"}}, pin="PIN"
    )
    item = client.get_series(1)
    assert calls[0] == ("POST", "login", None, {"apikey": "KEY", "pin": "PIN"})
    assert client._token == "TKN"
    assert client.session.headers["Authorization"] == "Bearer TKN"
    assert item.tvdb_id == 1 and item.media_type == "tv"
    client.get_series(2)  # second call reuses the token
    assert sum(1 for m, p, *_ in calls if p == "login") == 1


def test_login_omits_pin_when_absent():
    client, calls = _client(lambda m, p, par, j: {"data": {"id": 1, "name": "S"}})
    client.get_series(1)
    assert calls[0][3] == {"apikey": "KEY"}  # no pin key


def test_series_extended_pulls_remote_ids():
    payload = {
        "data": {
            "id": 9001,
            "name": "Example Show",
            "year": "2008",
            "firstAired": "2008-01-20",
            "remoteIds": [
                {"id": "9002", "sourceName": "TheMovieDB"},
                {"id": "tt9000001", "sourceName": "IMDB"},
            ],
        }
    }
    client, calls = _client(lambda m, p, par, j: payload)
    item = client.get_series(9001, extended=True)
    assert ("GET", "series/9001/extended", None, None) in calls
    assert item.tvdb_id == 9001 and item.tmdb_id == 9002 and item.imdb_id == "tt9000001"
    assert item.year == 2008 and item.release_date == "2008-01-20"


def test_tvdb_list_tv_reads_series_entities_in_order():
    def handler(method, path, params, json):
        assert path == "lists/777/extended"
        return {
            "data": {
                "entities": [
                    {"seriesId": 2, "order": 2},
                    {"seriesId": 1, "order": 1},
                    {"movieId": 9, "order": 3},  # ignored for tv
                ]
            }
        }

    client, _ = _client(handler)
    items = client.get_list(777, media="tv")
    assert [i.tvdb_id for i in items] == [1, 2]  # sorted by order
    assert all(i.media_type == "tv" for i in items)


def test_tvdb_list_movie_fetches_extended_movies():
    def handler(method, path, params, json):
        if path == "lists/5/extended":
            return {"data": {"entities": [{"movieId": 100, "order": 1}]}}
        assert path == "movies/100/extended"
        return {
            "data": {
                "id": 100,
                "name": "Film",
                "year": "2019",
                "remoteIds": [{"id": "603", "sourceName": "TheMovieDB"}],
            }
        }

    client, _ = _client(handler)
    items = client.get_list(5, media="movie")
    assert (
        items[0].tvdb_id == 100
        and items[0].tmdb_id == 603
        and items[0].media_type == "movie"
    )


def test_tvdb_list_resolves_slug():
    def handler(method, path, params, json):
        if path == "lists/slug/my-list":
            return {"data": {"id": 42}}
        assert path == "lists/42/extended"
        return {"data": {"entities": [{"seriesId": 7, "order": 1}]}}

    client, _ = _client(handler)
    assert client.get_list("my-list", media="tv")[0].tvdb_id == 7


def test_discover_defaults_country_lang_and_paginates():
    pages = {
        0: {"data": [{"id": 1, "name": "a"}], "links": {"next": "/p1"}},
        1: {"data": [{"id": 2, "name": "b"}], "links": {"next": None}},
    }
    seen_params: list = []

    def handler(method, path, params, json):
        assert path == "series/filter"
        seen_params.append(params)
        return pages[params["page"]]

    client, _ = _client(handler)
    items = client.discover({"genre": 3}, media="tv")
    assert [i.tvdb_id for i in items] == [1, 2]
    assert seen_params[0]["country"] == "usa" and seen_params[0]["lang"] == "eng"
    assert seen_params[0]["genre"] == 3


def test_resolve_name_searches_then_fetches_extended():
    def handler(method, path, params, json):
        if path == "search":
            assert params["type"] == "series" and params["query"] == "Example Show"
            return {
                "data": [{"tvdb_id": "9001", "name": "Example Show", "year": "2008"}]
            }
        assert path == "series/9001/extended"
        return {
            "data": {
                "id": 9001,
                "name": "Example Show",
                "remoteIds": [{"id": "9002", "sourceName": "TheMovieDB"}],
            }
        }

    client, _ = _client(handler)
    item = client.resolve("Example Show", media="tv")
    assert item.tvdb_id == 9001 and item.tmdb_id == 9002
