"""Offline unit tests for the foundation (no network)."""

from __future__ import annotations

import pytest

from nalanda.clients.tmdb import TMDBClient
from nalanda.config import Config, Secrets
from nalanda.models import MediaItem, MovieCollection


def test_v4_token_uses_bearer_auth():
    token = "eyJhbGciOi.JzdWIiOiQ.SflKxwRJ"  # JWT-shaped (two dots)
    client = TMDBClient(token)
    assert client.session.headers.get("Authorization") == f"Bearer {token}"
    assert "api_key" not in client._default_params


def test_v3_key_uses_query_param():
    key = "0123456789abcdef0123456789abcdef"  # 32-char hex, no dots
    client = TMDBClient(key)
    assert client._default_params.get("api_key") == key
    assert "Authorization" not in client.session.headers


def test_tmdb_client_has_rate_limiter():
    client = TMDBClient("0123456789abcdef0123456789abcdef")  # v3 key shape
    assert client._limiter is not None
    assert client._limiter._rate == 40.0


def test_tvdb_client_has_rate_limiter():
    from nalanda.clients.tvdb import TVDBClient

    client = TVDBClient()  # bundled key; login is lazy, so no network here
    assert client._limiter is not None
    assert client._limiter._rate == 10.0


def test_request_json_retries_on_5xx(monkeypatch):
    import time

    from nalanda.http import BaseClient

    class _Resp:
        def __init__(self, code):
            self.status_code = code
            self.ok = code < 400
            self.content = b"{}"
            self.url = "u"

        def json(self):
            return {"ok": True}

    client = BaseClient("https://example.test")
    seq = [_Resp(500), _Resp(503), _Resp(200)]  # two transient failures, then success
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append(path)
        return seq.pop(0)

    monkeypatch.setattr(client._session, "request", fake_request)
    monkeypatch.setattr(time, "sleep", lambda *_: None)  # don't actually wait
    assert client.request_json("GET", "/x") == {"ok": True}
    assert len(calls) == 3  # retried twice


def test_request_json_raises_after_exhausting_retries(monkeypatch):
    import time

    import pytest

    from nalanda.http import BaseClient, HTTPError

    class _Resp:
        status_code = 500
        ok = False
        content = b""
        url = "u"
        text = ""

    client = BaseClient("https://example.test")
    monkeypatch.setattr(client._session, "request", lambda *a, **k: _Resp())
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    with pytest.raises(HTTPError):
        client.request_json("GET", "/x")


def test_request_json_does_not_retry_post_on_5xx(monkeypatch):
    import time

    import pytest

    from nalanda.http import BaseClient, HTTPError

    class _Resp:
        status_code = 500
        ok = False
        content = b""
        url = "u"
        text = ""

    client = BaseClient("https://example.test")
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append(path)
        return _Resp()

    monkeypatch.setattr(client._session, "request", fake_request)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    # A 5xx may have been applied server-side, so a non-idempotent POST must NOT
    # be replayed.
    with pytest.raises(HTTPError):
        client.request_json("POST", "/x")
    assert len(calls) == 1


def test_request_json_retries_get_on_transport_error(monkeypatch):
    import time

    import niquests

    from nalanda.http import BaseClient

    class _Resp:
        status_code = 200
        ok = True
        content = b"{}"
        url = "u"

        def json(self):
            return {"ok": True}

    client = BaseClient("https://example.test")
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append(path)
        if len(calls) < 3:  # two transient read timeouts, then success
            raise niquests.exceptions.RequestException("read timed out")
        return _Resp()

    monkeypatch.setattr(client._session, "request", fake_request)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    assert client.request_json("GET", "/x") == {"ok": True}
    assert len(calls) == 3  # retried twice on the transport error


def test_request_json_does_not_retry_post_on_transport_error(monkeypatch):
    import time

    import niquests
    import pytest

    from nalanda.http import BaseClient, HTTPError

    client = BaseClient("https://example.test")
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append(path)
        raise niquests.exceptions.RequestException("read timed out")

    monkeypatch.setattr(client._session, "request", fake_request)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    # A POST that timed out may have been applied server-side -> must NOT be replayed.
    with pytest.raises(HTTPError):
        client.request_json("POST", "/x")
    assert len(calls) == 1


def test_request_json_retries_post_on_429(monkeypatch):
    import time

    from nalanda.http import BaseClient

    class _Resp:
        def __init__(self, code):
            self.status_code = code
            self.ok = code < 400
            self.content = b"{}"
            self.url = "u"
            self.text = ""

        def json(self):
            return {"ok": True}

    client = BaseClient("https://example.test")
    seq = [_Resp(429), _Resp(200)]  # rate-limited (not processed), then success
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append(path)
        return seq.pop(0)

    monkeypatch.setattr(client._session, "request", fake_request)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    # 429 means the request was never processed, so it is safe to replay for any method.
    assert client.request_json("POST", "/x") == {"ok": True}
    assert len(calls) == 2


def test_language_and_region_become_default_params():
    client = TMDBClient(
        "0123456789abcdef0123456789abcdef", language="fr-FR", region="GB"
    )
    assert client._default_params.get("language") == "fr-FR"
    assert client._default_params.get("region") == "GB"
    # region omitted by default
    assert (
        "region" not in TMDBClient("0123456789abcdef0123456789abcdef")._default_params
    )


def test_empty_token_rejected():
    import pytest

    with pytest.raises(ValueError):
        TMDBClient("")


def test_movie_from_tmdb_details():
    movie = MediaItem.from_tmdb(
        {
            "id": 9003,
            "title": "Example Film",
            "release_date": "2001-12-18",
            "imdb_id": "tt9000002",
            "overview": "A short overview.",
        }
    )
    assert movie.tmdb_id == 9003
    assert movie.year == 2001
    assert movie.release_date == "2001-12-18"
    assert movie.imdb_id == "tt9000002"


def test_movie_from_tmdb_external_ids_fallback_and_blank_date():
    movie = MediaItem.from_tmdb(
        {"id": 1, "title": "X", "release_date": "", "external_ids": {"imdb_id": "tt9"}}
    )
    assert movie.imdb_id == "tt9"
    assert movie.year is None


def test_collection_model():
    coll = MovieCollection(tmdb_id=1234, name="Example", movies=[MediaItem(title="A")])
    assert coll.tmdb_id == 1234
    assert len(coll.movies) == 1


def test_secrets_configured_flags(monkeypatch):
    for var in (
        "RADARR_URL",
        "RADARR_API_KEY",
        "JELLYFIN_URL",
        "JELLYFIN_API_KEY",
        "MDBLIST_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    secrets = Secrets(_env_file=None)
    assert secrets.radarr_configured is False
    assert secrets.jellyfin_configured is False
    assert secrets.mdblist_configured is False


def test_config_parses_minimal():
    cfg = Config.model_validate(
        {
            "collections": {
                "My Coll": {"media": "movie", "tmdb_collection": 1234, "overview": "hi"}
            }
        }
    )
    assert "My Coll" in cfg.collections
    assert cfg.settings.sync_mode == "append"


def test_config_rejects_unknown_collection_key():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(
        ValidationError
    ):  # typo'd key must be caught, not silently ignored
        Config.model_validate({"collections": {"C": {"tmdb_genra": "Western"}}})


def test_config_section_must_be_declared():
    import pytest
    from pydantic import ValidationError

    # a section that isn't in the top-level `sections` list is rejected
    with pytest.raises(ValidationError):
        Config.model_validate(
            {
                "sections": ["Charts"],
                "collections": {
                    "C": {
                        "media": "movie",
                        "tmdb_collection": 1,
                        "section": "Universes",
                    }
                },
            }
        )
    # a declared section is accepted
    cfg = Config.model_validate(
        {
            "sections": ["Charts"],
            "collections": {
                "C": {"media": "movie", "tmdb_collection": 1, "section": "Charts"}
            },
        }
    )
    assert cfg.collections["C"].section == "Charts"


def test_example_config_validates():
    from nalanda.config import load_config

    cfg = load_config("config.example.yml")  # the shipped example must match the schema
    assert cfg.collections


def test_secrets_nalanda_config_default(monkeypatch):
    monkeypatch.delenv("NALANDA_CONFIG", raising=False)
    assert (
        Secrets(_env_file=None).nalanda_config == "config.yml"
    )  # local-friendly default
    monkeypatch.setenv("NALANDA_CONFIG", "/config/config.yml")
    assert (
        Secrets(_env_file=None).nalanda_config == "/config/config.yml"
    )  # env override wins


def test_secrets_state_and_cache_derive_from_config_dir(monkeypatch):
    from pathlib import Path

    monkeypatch.setenv("NALANDA_CONFIG", "/config/config.yml")
    s = Secrets(_env_file=None)
    assert Path(s.nalanda_state) == Path("/config/.nalanda-state.json")
    assert Path(s.nalanda_cache) == Path("/config/.nalanda-cache.db")


def test_secrets_state_ignores_stray_nalanda_state_env(monkeypatch):
    from pathlib import Path

    monkeypatch.setenv("NALANDA_CONFIG", "/config/config.yml")
    monkeypatch.setenv(
        "NALANDA_STATE", "/somewhere/else/state.json"
    )  # no longer a knob
    s = Secrets(_env_file=None)
    assert Path(s.nalanda_state) == Path(
        "/config/.nalanda-state.json"
    )  # derived; env inert


def test_cache_settings_defaults_and_global_nesting():
    from nalanda.config import CacheSettings, GlobalSettings

    cs = CacheSettings()
    assert cs.enabled is True
    assert (cs.record_cache_duration, cs.list_cache_duration) == ("30d", "1d")
    assert (cs.query_cache_duration, cs.chart_cache_duration) == ("3d", "1d")
    assert GlobalSettings().cache.enabled is True  # present by default


def test_cache_settings_accepts_hours_and_off():
    from nalanda.config import CacheSettings

    cs = CacheSettings(query_cache_duration="6h", chart_cache_duration="off")
    assert cs.query_cache_duration == "6h"


@pytest.mark.parametrize("bad", ["5m", "90m", "1w", "nonsense"])
def test_cache_settings_rejects_bad_duration(bad):
    from pydantic import ValidationError

    from nalanda.config import CacheSettings

    with pytest.raises(ValidationError):
        CacheSettings(record_cache_duration=bad)


def test_cache_settings_rejects_unknown_key():
    from pydantic import ValidationError

    from nalanda.config import CacheSettings

    with pytest.raises(ValidationError):
        CacheSettings(nope=1)


def test_starter_template_validates():
    from importlib.resources import files

    from nalanda.config import load_config

    starter = files("nalanda.templates").joinpath("config.starter.yml")
    cfg = load_config(str(starter))  # the seeded starter must match the schema
    assert cfg.collections == {}


def test_starter_env_matches_example():
    # the bundled env template (seeded as .env) must not drift from the documented
    # .env.example
    from importlib.resources import files
    from pathlib import Path

    starter = files("nalanda.templates").joinpath("env.starter").read_text("utf-8")
    example = Path(".env.example").read_text("utf-8")
    assert starter == example


def test_json_schema_smoke():
    from nalanda.config import json_schema

    schema = json_schema()
    assert schema["title"] == "Nalanda configuration"
    assert schema["$schema"].startswith("https://json-schema.org/")
    # `except` alias (not the python-safe field name) must be what users write
    genre = schema["$defs"]["SelectFilter"]
    assert set(genre["properties"]) == {"all", "any", "except"}
    assert genre["additionalProperties"] is False
    assert schema["$defs"]["CollectionDef"]["additionalProperties"] is False


# ------------------------------------------------------------------ schedule cascade


def _coll(**extra):
    return {"media": "movie", "tmdb_collection": 1, **extra}


def test_schedule_bad_cron_rejected_at_each_level():
    import pytest
    from pydantic import ValidationError

    # global default
    with pytest.raises(ValidationError):
        Config.model_validate({"settings": {"run_schedule": "not a cron"}})
    # per-job-type default
    with pytest.raises(ValidationError):
        Config.model_validate({"settings": {"jobs": {"collections": "nope"}}})
    # per-collection
    with pytest.raises(ValidationError):
        Config.model_validate({"collections": {"A": _coll(run_schedule="xyz")}})
    # inside the named-schedules map
    with pytest.raises(ValidationError):
        Config.model_validate({"settings": {"run_schedules": {"daily": "bad"}}})


def test_schedule_unknown_named_ref_rejected():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Config.model_validate(
            {
                "settings": {"run_schedules": {"daily": "0 4 * * *"}},
                "collections": {
                    "A": _coll(run_schedule="weekly")
                },  # not a defined name
            }
        )


def test_schedule_unknown_job_kind_rejected():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(
        ValidationError
    ):  # only known kinds (collections, metadata) allowed under jobs
        Config.model_validate({"settings": {"jobs": {"bogus_kind": "0 4 * * *"}}})


def test_schedule_named_inline_and_sentinel_accepted():
    cfg = Config.model_validate(
        {
            "settings": {
                "run_schedules": {"daily": "0 4 * * *"},
                "run_schedule": "disabled",  # sentinel
                "jobs": {"collections": "0 5 * * *"},  # inline
            },
            "collections": {"A": _coll(run_schedule="daily")},  # named ref
        }
    )
    assert cfg.collections["A"].run_schedule == "daily"


def test_resolve_schedules_three_level_cascade():
    cfg = Config.model_validate(
        {
            "settings": {
                "run_schedules": {"daily": "0 4 * * *", "hourly": "0 * * * *"},
                "run_schedule": "daily",
                "jobs": {"collections": "daily"},
            },
            "collections": {
                "A": _coll(run_schedule="hourly"),  # own override
                "B": _coll(run_schedule="*/30 * * * *"),  # own inline
                "C": _coll(run_schedule="none"),  # opt out
                "D": _coll(),  # inherit default
            },
        }
    )
    groups, default_cron = cfg.resolve_schedules("collections")
    assert default_cron == "0 4 * * *"
    assert {cron: sorted(names) for cron, names in groups.items()} == {
        "0 * * * *": ["A"],
        "*/30 * * * *": ["B"],
        "0 4 * * *": ["D"],  # C is opted out and absent
    }


def test_resolve_schedules_job_default_overrides_global():
    cfg = Config.model_validate(
        {
            "settings": {
                "run_schedules": {"daily": "0 4 * * *", "hourly": "0 * * * *"},
                "run_schedule": "daily",
                "jobs": {"collections": "hourly"},  # beats the global default
            },
            "collections": {"A": _coll()},
        }
    )
    groups, default_cron = cfg.resolve_schedules("collections")
    assert default_cron == "0 * * * *"
    assert sorted(groups["0 * * * *"]) == ["A"]


def test_resolve_schedules_all_individual_means_no_default():
    cfg = Config.model_validate(
        {
            "collections": {
                "A": _coll(run_schedule="0 * * * *"),
                "B": _coll(run_schedule="0 2 * * *"),
            }
        }
    )
    groups, default_cron = cfg.resolve_schedules("collections")
    assert default_cron is None  # nothing prunes on a schedule -> serve warns
    assert {cron: sorted(n) for cron, n in groups.items()} == {
        "0 * * * *": ["A"],
        "0 2 * * *": ["B"],
    }


def test_resolve_schedules_no_schedules_at_all():
    cfg = Config.model_validate({"collections": {"A": _coll()}})
    groups, default_cron = cfg.resolve_schedules("collections")
    assert groups == {}
    assert default_cron is None


def test_arr_dtos_are_shared_aliases():
    from nalanda.models import (
        ArrQualityProfile,
        ArrRootFolder,
        ArrTag,
        RadarrQualityProfile,
        RadarrRootFolder,
        RadarrTag,
        SonarrQualityProfile,
        SonarrRootFolder,
        SonarrTag,
    )

    assert RadarrTag is ArrTag is SonarrTag
    assert RadarrQualityProfile is ArrQualityProfile is SonarrQualityProfile
    assert RadarrRootFolder is ArrRootFolder is SonarrRootFolder
    assert ArrTag.from_arr({"id": 3, "label": "x"}) == ArrTag(id=3, label="x")
    assert ArrQualityProfile.from_arr({"id": 1, "name": "HD"}).name == "HD"
    rf = ArrRootFolder.from_arr({"id": 1, "path": "/m", "accessible": True})
    assert rf.path == "/m" and rf.accessible is True


def test_resolve_schedules_sentinel_is_case_insensitive():
    cfg = Config.model_validate(
        {
            "settings": {"run_schedule": "0 4 * * *"},
            "collections": {
                "A": _coll(run_schedule="Disabled"),  # mixed case
                "B": _coll(run_schedule="NONE "),  # trailing space
                "C": _coll(),  # inherits default
            },
        }
    )
    groups, default_cron = cfg.resolve_schedules("collections")
    assert default_cron == "0 4 * * *"
    assert {c: sorted(n) for c, n in groups.items()} == {"0 4 * * *": ["C"]}


def test_resolve_schedules_explicit_default_cron_joins_prune_group():
    # A collection that explicitly sets the same cron as the default joins the default
    # (pruning) group rather than spawning a duplicate timer.
    cfg = Config.model_validate(
        {
            "settings": {
                "run_schedules": {"daily": "0 4 * * *"},
                "run_schedule": "daily",
            },
            "collections": {
                "A": _coll(run_schedule="0 4 * * *"),  # inline, equals the default
                "B": _coll(),  # inherits the default
            },
        }
    )
    groups, default_cron = cfg.resolve_schedules("collections")
    assert default_cron == "0 4 * * *"
    assert sorted(groups["0 4 * * *"]) == ["A", "B"]
    assert len(groups) == 1  # no duplicate group/timer for A


def test_inherited_field_sets_cannot_drift():
    from nalanda.config import (
        _RADARR_INHERITED,
        _SONARR_INHERITED,
        RadarrDefaults,
        RadarrOptions,
        SonarrDefaults,
        SonarrOptions,
    )

    assert set(_RADARR_INHERITED) == set(RadarrOptions.model_fields) - {"enable", "tag"}
    assert set(_RADARR_INHERITED) <= set(RadarrDefaults.model_fields)
    assert set(_SONARR_INHERITED) == set(SonarrOptions.model_fields) - {"enable", "tag"}
    assert set(_SONARR_INHERITED) <= set(SonarrDefaults.model_fields)


def test_rate_limiter_blocks_when_drained(monkeypatch):
    import nalanda.http as http

    clock = {"t": 1000.0}
    sleeps = []
    monkeypatch.setattr(http.time, "monotonic", lambda: clock["t"])

    def fake_sleep(secs):
        sleeps.append(secs)
        clock["t"] += secs  # advance the clock as if we really slept

    monkeypatch.setattr(http.time, "sleep", fake_sleep)

    rl = http.RateLimiter(rate=2, burst=2)  # bucket of 2, refills 2/sec
    rl.acquire()  # 2 -> 1 token, no wait
    rl.acquire()  # 1 -> 0 token, no wait
    assert sleeps == []
    rl.acquire()  # drained: wait (1-0)/2 = 0.5s, then proceed
    assert sleeps == [0.5]


def test_rate_limiter_set_rate_changes_pacing(monkeypatch):
    import nalanda.http as http

    clock = {"t": 0.0}
    sleeps = []
    monkeypatch.setattr(http.time, "monotonic", lambda: clock["t"])

    def fake_sleep(secs):
        sleeps.append(secs)
        clock["t"] += secs

    monkeypatch.setattr(http.time, "sleep", fake_sleep)

    rl = http.RateLimiter(rate=1, burst=1)
    rl.acquire()  # drain the single token, no wait
    rl.set_rate(10)  # 10/sec now; tokens clamped to 0
    rl.acquire()  # wait (1-0)/10 = 0.1s
    assert sleeps == [0.1]


def test_retry_after_honors_integer_header():
    from nalanda.http import _retry_after

    class _R:
        headers = {"Retry-After": "5"}

    assert _retry_after(_R(), 0) == 5.0


def test_retry_after_caps_huge_value():
    from nalanda.http import RETRY_AFTER_CAP, _retry_after

    class _R:
        headers = {"Retry-After": "99999"}

    assert _retry_after(_R(), 0) == RETRY_AFTER_CAP


def test_retry_after_falls_back_to_exponential():
    from nalanda.http import RETRY_BACKOFF, _retry_after

    class _R:
        headers = {}  # no Retry-After

    assert _retry_after(_R(), 2) == RETRY_BACKOFF * 4  # 0.5 * 2**2


def test_retry_after_ignores_non_integer_header():
    from nalanda.http import RETRY_BACKOFF, _retry_after

    class _R:
        # An HTTP-date Retry-After, not integer seconds.
        headers = {"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}

    # A non-integer header can't parse -> fall through to exponential backoff.
    assert _retry_after(_R(), 2) == RETRY_BACKOFF * 4


def test_baseclient_acquires_before_each_send(monkeypatch):
    import time

    from nalanda.http import BaseClient

    class _Resp:
        def __init__(self, code, headers=None):
            self.status_code = code
            self.ok = code < 400
            self.content = b"{}"
            self.url = "u"
            self.text = ""
            self.headers = headers or {}

        def json(self):
            return {"ok": True}

    class _Limiter:
        def __init__(self):
            self.acquired = 0

        def acquire(self):
            self.acquired += 1

    limiter = _Limiter()
    client = BaseClient("https://example.test", limiter=limiter)
    seq = [_Resp(429, {"Retry-After": "1"}), _Resp(200)]
    sends = []

    def fake_request(method, path, **kwargs):
        sends.append(path)
        return seq.pop(0)

    monkeypatch.setattr(client._session, "request", fake_request)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    assert client.request_json("GET", "/x") == {"ok": True}
    assert len(sends) == 2  # retried once
    assert limiter.acquired == 2  # acquired before every send, including the retry


def test_baseclient_honors_retry_after_on_429(monkeypatch):
    import time

    from nalanda.http import BaseClient

    class _Resp:
        def __init__(self, code, headers=None):
            self.status_code = code
            self.ok = code < 400
            self.content = b"{}"
            self.url = "u"
            self.text = ""
            self.headers = headers or {}

        def json(self):
            return {"ok": True}

    client = BaseClient("https://example.test")
    seq = [_Resp(429, {"Retry-After": "7"}), _Resp(200)]
    waits = []

    def fake_request(method, path, **kwargs):
        return seq.pop(0)

    monkeypatch.setattr(client._session, "request", fake_request)
    monkeypatch.setattr(time, "sleep", lambda secs: waits.append(secs))
    assert client.request_json("GET", "/x") == {"ok": True}
    assert waits == [7.0]  # honored the Retry-After header, not the 0.5s default


def test_post_bytes_acquires_and_honors_retry_after(monkeypatch):
    import time

    from nalanda.http import BaseClient

    class _Resp:
        def __init__(self, code, headers=None):
            self.status_code = code
            self.ok = code < 400
            self.content = b""
            self.url = "u"
            self.text = ""
            self.headers = headers or {}

    class _Limiter:
        def __init__(self):
            self.acquired = 0

        def acquire(self):
            self.acquired += 1

    limiter = _Limiter()
    client = BaseClient("https://example.test", limiter=limiter)
    seq = [_Resp(429, {"Retry-After": "3"}), _Resp(200)]
    sends = []
    waits = []

    def fake_request(method, path, **kwargs):
        sends.append(method)
        return seq.pop(0)

    monkeypatch.setattr(client._session, "request", fake_request)
    monkeypatch.setattr(time, "sleep", lambda secs: waits.append(secs))
    client.post_bytes("/x", b"bytes", content_type="image/jpeg")
    assert len(sends) == 2  # retried once after 429
    assert limiter.acquired == 2  # acquired before each send incl. the retry
    assert waits == [3.0]  # honored Retry-After on the 429


def test_mdblist_client_default_rate():
    from nalanda.clients.mdblist import MDBListClient

    client = MDBListClient("key")
    assert client._limiter is not None
    assert client._limiter._rate == 1.0


def test_mdblist_supporter_probe_bumps_rate(monkeypatch):
    from nalanda.clients.mdblist import MDBListClient

    client = MDBListClient("key")

    def fake_get(path, **kwargs):
        assert path == "user"
        return {"is_supporter": True, "api_requests": 1000, "api_requests_count": 5}

    monkeypatch.setattr(client, "get", fake_get)
    client._ensure_supporter_checked()
    assert client._supporter is True
    assert client._limiter._rate == 5.0


def test_mdblist_supporter_probe_runs_once(monkeypatch):
    from nalanda.clients.mdblist import MDBListClient

    client = MDBListClient("key")
    calls = []

    def fake_get(path, **kwargs):
        calls.append(path)
        return {"is_supporter": False}

    monkeypatch.setattr(client, "get", fake_get)
    client._ensure_supporter_checked()
    client._ensure_supporter_checked()
    assert calls == ["user"]  # probed only once


def test_mdblist_probe_degrades_on_error(monkeypatch):
    from nalanda.clients.mdblist import MDBListClient
    from nalanda.http import HTTPError

    client = MDBListClient("key")
    calls = []

    def boom(path, **kwargs):
        calls.append(path)
        raise HTTPError("nope")

    monkeypatch.setattr(client, "get", boom)
    client._ensure_supporter_checked()  # must not raise
    client._ensure_supporter_checked()  # must not re-probe after a failure
    assert calls == ["user"]  # probed exactly once despite the error
    assert client._supporter_checked is True
    assert client._limiter._rate == 1.0  # rate unchanged


def test_mdblist_probe_degrades_on_malformed_json(monkeypatch):
    # A 200 OK /user with a non-JSON body surfaces as ValueError (JSONDecodeError);
    # the probe must absorb it and keep the non-supporter rate.
    from nalanda.clients.mdblist import MDBListClient

    client = MDBListClient("key")

    def bad_json(path, **kwargs):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

    monkeypatch.setattr(client, "get", bad_json)
    client._ensure_supporter_checked()  # must not raise
    assert client._limiter._rate == 1.0  # rate unchanged


def test_mdblist_quota_error_is_distinct():
    import pytest

    from nalanda.clients.mdblist import MDBListClient, MDBListLimitReached

    with pytest.raises(MDBListLimitReached):
        MDBListClient._items(
            {"response": False, "error": "API Rate Limit Reached!"}, "list"
        )
