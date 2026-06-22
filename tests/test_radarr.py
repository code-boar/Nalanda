"""Offline unit tests for the Radarr client models and helpers (no network)."""

from __future__ import annotations

import pytest

from nalanda.clients.radarr import RadarrClient, index_movies_by_tmdb
from nalanda.models import (
    RadarrMovie,
    RadarrQualityProfile,
    RadarrRootFolder,
    RadarrTag,
)


def test_client_requires_creds():
    with pytest.raises(ValueError):
        RadarrClient("", "key")
    with pytest.raises(ValueError):
        RadarrClient("http://host", "")


def test_client_sets_api_key_header():
    client = RadarrClient("http://host:7878", "SECRET")
    assert client.session.headers.get("X-Api-Key") == "SECRET"


def test_movie_from_radarr():
    movie = RadarrMovie.from_radarr(
        {
            "id": 12,
            "tmdbId": 9006,
            "imdbId": "tt9000003",
            "title": "Example Film",
            "year": 2010,
            "qualityProfileId": 27,
            "monitored": True,
            "hasFile": True,
            "tags": [433, 435, 1],
        }
    )
    assert movie.id == 12
    assert movie.tmdb_id == 9006
    assert movie.tags == [433, 435, 1]
    assert movie.quality_profile_id == 27
    assert movie.monitored and movie.has_file


def test_lookup_result_without_id_normalised_to_none():
    # Radarr returns id 0 for a movie not yet in the library.
    movie = RadarrMovie.from_radarr(
        {"id": 0, "tmdbId": 999, "title": "New", "year": 2030}
    )
    assert movie.id is None
    assert movie.tmdb_id == 999
    assert movie.tags == []


def test_simple_resource_parsing():
    assert (
        RadarrQualityProfile.from_arr({"id": 27, "name": "[SQP] SQP-3"}).name
        == "[SQP] SQP-3"
    )
    assert RadarrTag.from_arr({"id": 452, "label": "other-bond"}).label == "other-bond"
    rf = RadarrRootFolder.from_arr(
        {"id": 1, "path": "/data/media/movies", "accessible": True}
    )
    assert rf.path == "/data/media/movies" and rf.accessible is True


def test_index_movies_by_tmdb_skips_missing_tmdb():
    movies = [
        RadarrMovie(id=1, tmdb_id=11, title="A"),
        RadarrMovie(id=2, tmdb_id=None, title="B"),  # no tmdb id -> skipped
        RadarrMovie(id=3, tmdb_id=22, title="C"),
    ]
    index = index_movies_by_tmdb(movies)
    assert set(index) == {11, 22}
    assert index[11].title == "A"
