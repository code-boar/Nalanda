"""Offline tests for the TMDB collection-image adapter (`get_collection`).

The per-slot *ranking* now lives in `nalanda.art_select` (see test_art_select.py);
these tests cover the TMDB-specific wiring: requesting the right image languages
(the locale gotcha) and mapping the response onto the Primary/Thumb/Backdrop slots
with the top-level fallbacks intact.
"""

from __future__ import annotations

from nalanda.clients.tmdb import TMDBClient


def _client(language="en-US"):
    # JWT-shaped dummy token; get_collection is stubbed below, so no network call
    # happens.
    return TMDBClient("k.k.k", language=language)


def _stub_get(client, payload):
    captured: dict = {}

    def fake_get(path, params=None, **_):
        captured["path"] = path
        captured["params"] = params or {}
        return payload

    client.get = fake_get  # type: ignore[method-assign]
    return captured


# --- the image-language request (TMDB locale gotcha) -------------------------
# TMDB filters appended images by the request `language`; image langs are bare
# 2-letter (`en`), so `language=en-GB` matches none and silently drops every
# titled backdrop. We pass `include_image_language` (preferred 2-letter + en +
# null) so selection stays locale-proof.

_COLLECTION_PAYLOAD = {
    "id": 1,
    "name": "X",
    "overview": "o",
    "poster_path": "/poster.jpg",
    "backdrop_path": "/top-level.jpg",
    "parts": [],
    "images": {
        "backdrops": [
            {
                "file_path": "/titled-en.jpg",
                "iso_639_1": "en",
                "vote_average": 7,
                "vote_count": 10,
            },
            {
                "file_path": "/textless.jpg",
                "iso_639_1": None,
                "vote_average": 8,
                "vote_count": 20,
            },
        ]
    },
}


def test_get_collection_requests_image_languages_locale_proof():
    c = _client("en-GB")
    captured = _stub_get(c, _COLLECTION_PAYLOAD)
    coll = c.get_collection(1)
    # en-GB -> preferred subtag 'en'; deduped to "en,null"
    assert captured["params"]["include_image_language"] == "en,null"
    assert captured["params"]["append_to_response"] == "images"
    # the titled (en) backdrop resolves as the Thumb instead of being lost
    assert coll.thumb_path == "/titled-en.jpg"
    # and the textless one (from images, best-rated) is the Backdrop
    assert coll.backdrop_path == "/textless.jpg"


def test_get_collection_image_languages_for_non_english_locale():
    c = _client("fr-FR")
    captured = _stub_get(c, _COLLECTION_PAYLOAD)
    c.get_collection(1)
    assert captured["params"]["include_image_language"] == "fr,en,null"


# --- slot mapping parity (the behaviour the old _pick_backdrop guaranteed) ----

_MIXED_PAYLOAD = {
    "id": 2,
    "name": "Mixed",
    "overview": "o",
    "poster_path": "/poster.jpg",
    "backdrop_path": "/top-level.jpg",
    "parts": [],
    "images": {
        "backdrops": [
            {
                "file_path": "/textless-low.jpg",
                "iso_639_1": None,
                "vote_average": 5.0,
                "vote_count": 3,
            },
            {
                "file_path": "/textless-high.jpg",
                "iso_639_1": "xx",
                "vote_average": 8.0,
                "vote_count": 9,
            },
            {
                "file_path": "/en.jpg",
                "iso_639_1": "en",
                "vote_average": 7.0,
                "vote_count": 50,
            },
            {
                "file_path": "/fr.jpg",
                "iso_639_1": "fr",
                "vote_average": 9.5,
                "vote_count": 99,
            },
        ]
    },
}


def test_get_collection_selects_expected_slots():
    c = _client("en-US")
    _stub_get(c, _MIXED_PAYLOAD)
    coll = c.get_collection(2)
    # Primary = the collection's top-level poster (not picked from posters[])
    assert coll.poster_path == "/poster.jpg"
    # Backdrop = best-rated textless (xx counts as textless; 8.0 beats 5.0)
    assert coll.backdrop_path == "/textless-high.jpg"
    # Thumb = preferred-language titled (en) beats higher-rated other-lang (fr)
    assert coll.thumb_path == "/en.jpg"


def test_get_collection_falls_back_when_images_empty():
    # No backdrops in images -> Thumb is None, Backdrop falls back to the
    # top-level field.
    payload = {
        "id": 3,
        "name": "Bare",
        "overview": None,
        "poster_path": "/poster.jpg",
        "backdrop_path": "/top-level.jpg",
        "parts": [],
        "images": {"backdrops": []},
    }
    c = _client("en-US")
    _stub_get(c, payload)
    coll = c.get_collection(3)
    assert coll.poster_path == "/poster.jpg"
    assert coll.thumb_path is None
    assert coll.backdrop_path == "/top-level.jpg"
