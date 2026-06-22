"""Offline tests for the Radarr client write primitives (no network).

Each test instantiates a real ``RadarrClient`` and shadows its ``get``/``post``/``put``
methods with recording fakes, so we exercise the request shaping without any HTTP.
"""

from __future__ import annotations

import pytest

from nalanda.clients.radarr import RadarrClient


def _client() -> RadarrClient:
    return RadarrClient("http://host:7878", "key")


# ---------------------------------------------------------------- ensure_tag


def test_ensure_tag_returns_existing_case_insensitive():
    c = _client()
    c.get = lambda path, **kw: [{"id": 5, "label": "Nalanda-Example"}]
    c.post = lambda *a, **k: pytest.fail("should not create an existing tag")
    assert c.ensure_tag("nalanda-example") == 5


def test_ensure_tag_creates_when_absent():
    c = _client()
    c.get = lambda path, **kw: [{"id": 1, "label": "other"}]
    posts: list[tuple] = []
    c.post = lambda path, **kw: posts.append((path, kw)) or {"id": 77}
    assert c.ensure_tag("nalanda-new") == 77
    assert posts[0][0].endswith("/tag")
    assert posts[0][1]["json"] == {"label": "nalanda-new"}


# ------------------------------------------------------------------ add_movie


def test_add_movie_builds_add_payload():
    c = _client()
    c.get = lambda path, **kw: {
        "tmdbId": 9006,
        "title": "Example Film",
        "titleSlug": "example-film-9006",
        "year": 2010,
        "id": 0,
    }
    posts: list[tuple] = []

    def fake_post(path, **kw):
        posts.append((path, kw))
        return {"id": 12, "tmdbId": 9006, "title": "Example Film", "year": 2010}

    c.post = fake_post
    movie = c.add_movie(
        9006,
        quality_profile_id=4,
        root_folder="/movies",
        monitored=True,
        minimum_availability="in_cinemas",
        tag_ids=[7],
        search=True,
    )
    assert movie is not None and movie.id == 12
    path, kw = posts[0]
    assert path.endswith("/movie")
    body = kw["json"]
    assert body["qualityProfileId"] == 4
    assert body["rootFolderPath"] == "/movies"
    assert body["monitored"] is True
    assert (
        body["minimumAvailability"] == "inCinemas"
    )  # config value mapped to Radarr spelling
    assert body["tags"] == [7]
    assert body["addOptions"] == {"searchForMovie": True}


def test_add_movie_returns_none_for_unknown_tmdb():
    c = _client()
    c.get = lambda path, **kw: None  # lookup miss
    c.post = lambda *a, **k: pytest.fail("should not POST when lookup fails")
    assert c.add_movie(1, quality_profile_id=1, root_folder="/m") is None


# ----------------------------------------------------------------- edit_movies


def test_edit_movies_tag_body_and_batching():
    c = _client()
    puts: list[tuple] = []
    c.put = lambda path, **kw: puts.append((path, kw))
    c.edit_movies(list(range(250)), tags=[9], apply_tags="add")
    assert [len(p[1]["json"]["movieIds"]) for p in puts] == [100, 100, 50]
    body = puts[0][1]["json"]
    assert body["tags"] == [9]
    assert body["applyTags"] == "add"
    assert "qualityProfileId" not in body


def test_edit_movies_profile_and_monitored():
    c = _client()
    puts: list[tuple] = []
    c.put = lambda path, **kw: puts.append((path, kw))
    c.edit_movies([1, 2], quality_profile_id=5, monitored=False)
    body = puts[0][1]["json"]
    assert body["movieIds"] == [1, 2]
    assert body["qualityProfileId"] == 5
    assert body["monitored"] is False
    assert "applyTags" not in body


def test_edit_movies_noop_on_empty_ids_or_no_fields():
    c = _client()
    c.put = lambda *a, **k: pytest.fail("should not PUT with nothing to do")
    c.edit_movies([])  # no ids
    c.edit_movies([1, 2])  # ids but no field to change
