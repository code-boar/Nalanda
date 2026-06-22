"""Offline tests for the Sonarr client write primitives + models (no network)."""

from __future__ import annotations

import pytest

from nalanda.clients.sonarr import SonarrClient, index_series_by_ids, sonarr_monitor
from nalanda.models import (
    SonarrQualityProfile,
    SonarrRootFolder,
    SonarrSeries,
    SonarrTag,
)


def _client() -> SonarrClient:
    return SonarrClient("http://host:8989", "key")


def test_client_requires_creds():
    with pytest.raises(ValueError):
        SonarrClient("", "key")
    with pytest.raises(ValueError):
        SonarrClient("http://host", "")


def test_client_sets_api_key_header():
    assert _client().session.headers.get("X-Api-Key") == "key"


def test_monitor_mapping():
    assert sonarr_monitor("first_season") == "firstSeason"
    assert sonarr_monitor("latest_season") == "latestSeason"
    assert sonarr_monitor("all") == "all"


# ------------------------------------------------------------------ models


def test_series_from_sonarr():
    s = SonarrSeries.from_sonarr(
        {
            "id": 12,
            "tvdbId": 9001,
            "tmdbId": 9002,
            "imdbId": "tt9000001",
            "title": "Example Show",
            "monitored": True,
            "qualityProfileId": 4,
            "languageProfileId": 1,
            "seriesType": "standard",
            "seasonFolder": True,
            "seasons": [
                {"seasonNumber": 0, "monitored": False},
                {"seasonNumber": 1, "monitored": True},
            ],
            "tags": [7, 9],
        }
    )
    assert s.id == 12 and s.tvdb_id == 9001 and s.tmdb_id == 9002
    assert s.monitored and s.quality_profile_id == 4 and s.language_profile_id == 1
    assert [(x.season_number, x.monitored) for x in s.seasons] == [
        (0, False),
        (1, True),
    ]
    assert s.tags == [7, 9]


def test_lookup_result_without_id_normalised_to_none():
    s = SonarrSeries.from_sonarr({"id": 0, "tvdbId": 999, "title": "New"})
    assert s.id is None and s.tvdb_id == 999


def test_simple_resource_parsing():
    assert (
        SonarrQualityProfile.from_arr({"id": 4, "name": "HD-1080p"}).name == "HD-1080p"
    )
    assert SonarrTag.from_arr({"id": 1, "label": "nalanda-x"}).label == "nalanda-x"
    assert (
        SonarrRootFolder.from_arr({"id": 1, "path": "/tv", "accessible": True}).path
        == "/tv"
    )


# ------------------------------------------------------------------ ensure_tag


def test_ensure_tag_existing_case_insensitive():
    c = _client()
    c.get = lambda path, **kw: [{"id": 5, "label": "Nalanda-Show"}]
    c.post = lambda *a, **k: pytest.fail("should not create an existing tag")
    assert c.ensure_tag("nalanda-show") == 5


def test_ensure_tag_creates_when_absent():
    c = _client()
    c.get = lambda path, **kw: [{"id": 1, "label": "other"}]
    posts: list = []
    c.post = lambda path, **kw: posts.append((path, kw)) or {"id": 88}
    assert c.ensure_tag("nalanda-new") == 88
    assert posts[0][1]["json"] == {"label": "nalanda-new"}


# ------------------------------------------------------------------ add_series


def test_add_series_builds_payload_with_monitor_mapping():
    c = _client()
    c.lookup = lambda term: [
        {"tvdbId": 9001, "title": "BB", "seasons": [{"seasonNumber": 1}]}
    ]
    posts: list = []
    c.post = lambda path, **kw: (
        posts.append((path, kw)) or {"id": 7, "tvdbId": 9001, "title": "BB"}
    )
    series = c.add_series(
        "tvdb:9001",
        quality_profile_id=4,
        root_folder="/tv",
        language_profile_id=1,
        monitored=True,
        monitor="first_season",
        series_type="anime",
        season_folder=False,
        tag_ids=[7],
        search=True,
        cutoff_search=True,
    )
    assert series is not None and series.id == 7
    path, kw = posts[0]
    assert path.endswith("/series")
    body = kw["json"]
    assert body["qualityProfileId"] == 4 and body["rootFolderPath"] == "/tv"
    assert body["languageProfileId"] == 1
    assert (
        body["monitored"] is True
        and body["seriesType"] == "anime"
        and body["seasonFolder"] is False
    )
    assert body["tags"] == [7]
    assert body["addOptions"] == {
        "monitor": "firstSeason",
        "searchForMissingEpisodes": True,
        "searchForCutoffUnmetEpisodes": True,
    }


def test_add_series_returns_none_when_lookup_empty():
    c = _client()
    c.get = lambda path, **kw: []  # lookup miss
    c.post = lambda *a, **k: pytest.fail("should not POST when lookup fails")
    assert c.add_series("tvdb:1", quality_profile_id=1, root_folder="/tv") is None


# ----------------------------------------------------------------- edit_series


def test_edit_series_tag_body_and_batching():
    c = _client()
    puts: list = []
    c.put = lambda path, **kw: puts.append((path, kw))
    c.edit_series(list(range(250)), tags=[9], apply_tags="add")
    assert [len(p[1]["json"]["seriesIds"]) for p in puts] == [100, 100, 50]
    body = puts[0][1]["json"]
    assert body["tags"] == [9] and body["applyTags"] == "add"
    assert "qualityProfileId" not in body


def test_edit_series_profile_monitored_type():
    c = _client()
    puts: list = []
    c.put = lambda path, **kw: puts.append((path, kw))
    c.edit_series([1, 2], quality_profile_id=5, monitored=False, series_type="daily")
    body = puts[0][1]["json"]
    assert body["seriesIds"] == [1, 2]
    assert (
        body["qualityProfileId"] == 5
        and body["monitored"] is False
        and body["seriesType"] == "daily"
    )


def test_edit_series_noop_on_empty_or_no_fields():
    c = _client()
    c.put = lambda *a, **k: pytest.fail("should not PUT with nothing to do")
    c.edit_series([])
    c.edit_series([1, 2])


# ----------------------------------------------------------------- set_seasons


def _series_raw():
    return {
        "id": 10,
        "title": "BB",
        "seasons": [
            {"seasonNumber": 1, "monitored": False},
            {"seasonNumber": 2, "monitored": True},
        ],
    }


def test_set_seasons_writes_only_on_difference():
    c = _client()
    c.get = lambda path, **kw: _series_raw()
    puts: list = []
    c.put = lambda path, **kw: puts.append((path, kw))
    assert c.set_seasons(10, {1: True, 2: True}) is True
    body = puts[0][1]["json"]
    assert [s["monitored"] for s in body["seasons"]] == [True, True]
    assert puts[0][0].endswith("/series/10")


def test_set_seasons_noop_when_already_matching():
    c = _client()
    c.get = lambda path, **kw: _series_raw()
    c.put = lambda *a, **k: pytest.fail("should not PUT when seasons already match")
    assert c.set_seasons(10, {1: False, 2: True}) is False


# ------------------------------------------------------- language profiles / index


def test_language_profiles_tolerate_404():
    c = _client()
    c.get = lambda path, **kw: None  # v4: endpoint gone -> not_found_ok returns None
    assert c.get_language_profiles() == []


def test_index_series_by_ids_multi_key():
    index = index_series_by_ids(
        [
            SonarrSeries(id=1, tvdb_id=11, tmdb_id=111, title="A"),
            SonarrSeries(id=2, tvdb_id=22, imdb_id="tt22", title="B"),
        ]
    )
    assert index["tvdb"][11].id == 1 and index["tmdb"][111].id == 1
    assert index["tvdb"][22].id == 2 and index["imdb"]["tt22"].id == 2
