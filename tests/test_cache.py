"""Tests for the metadata cache (nalanda/cache.py). All offline, tmp_path-backed."""

from __future__ import annotations

import json
import sqlite3

import pytest

from nalanda import cache as cache_mod
from nalanda.cache import Cache, parse_duration, ttl_map


class _Clock:
    """A controllable stand-in for time.time()."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _no_jitter(monkeypatch) -> None:
    monkeypatch.setattr(cache_mod.random, "uniform", lambda a, b: 0.0)


# --- parse_duration / ttl_map ------------------------------------------------


def test_parse_duration_valid():
    assert parse_duration("30d") == 30 * 86400
    assert parse_duration("6h") == 6 * 3600
    assert parse_duration("0") == 0.0
    assert parse_duration("off") == 0.0
    assert parse_duration("") == 0.0  # lenient: empty -> bypass


@pytest.mark.parametrize("bad", ["5m", "90m", "x", "1w", "-3d", "d", "h", "3.5d"])
def test_parse_duration_rejects(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


def test_ttl_map_buckets_and_arr_constants():
    class CS:
        record_cache_duration = "30d"
        list_cache_duration = "1d"
        query_cache_duration = "3d"
        chart_cache_duration = "0"  # bypass

    m = ttl_map(CS())
    assert m["tmdb.record"] == 30 * 86400
    assert m["tvdb.record"] == 30 * 86400
    assert m["mdblist.list"] == 86400
    assert m["tmdb.query"] == 3 * 86400
    assert m["tmdb.chart"] == 0.0
    assert m["radarr.lookup"] == cache_mod.LOOKUP_TTL
    assert m["sonarr.lookup_empty"] == cache_mod.LOOKUP_EMPTY_TTL
    assert m["radarr.add_failed"] == cache_mod.ADD_FAILED_TTL


# --- fetch round-trip + counters ---------------------------------------------


def test_fetch_round_trip_loads_once(tmp_path):
    c = Cache(tmp_path / "c.db", ttls={"tmdb.record": 100})
    calls: list[int] = []

    def get():
        return c.fetch(
            "tmdb.record",
            "k",
            ttl=100,
            loader=lambda: calls.append(1) or {"a": [1, 2, 3]},
            dump=json.dumps,
            load=json.loads,
        )

    assert get() == {"a": [1, 2, 3]}  # miss
    assert get() == {"a": [1, 2, 3]}  # hit
    assert len(calls) == 1


def test_summary_hit_expired_miss(tmp_path, monkeypatch):
    _no_jitter(monkeypatch)
    clock = _Clock(1000.0)
    monkeypatch.setattr(cache_mod.time, "time", clock)
    c = Cache(tmp_path / "c.db", ttls={"tmdb.record": 100})
    c.fetch("tmdb.record", "k", ttl=100, loader=lambda: {"x": 1})  # miss
    c.fetch("tmdb.record", "k", ttl=100, loader=lambda: {"x": 1})  # hit
    clock.t = 1200  # past the 100s ttl
    c.fetch("tmdb.record", "k", ttl=100, loader=lambda: {"x": 1})  # expired
    assert c.summary == "1 hit, 1 expired, 1 miss"


def test_ttl_expiry_boundary(tmp_path, monkeypatch):
    _no_jitter(monkeypatch)
    clock = _Clock(1000.0)
    monkeypatch.setattr(cache_mod.time, "time", clock)
    c = Cache(tmp_path / "c.db", ttls={"tmdb.record": 100})
    calls: list[int] = []
    load = lambda: calls.append(1) or {"x": 1}  # noqa: E731
    c.fetch("tmdb.record", "k", ttl=100, loader=load)  # stored at t=1000
    clock.t = 1100  # exactly ttl -> still fresh (<=)
    c.fetch("tmdb.record", "k", ttl=100, loader=load)
    assert len(calls) == 1
    clock.t = 1101  # just past -> expired
    c.fetch("tmdb.record", "k", ttl=100, loader=load)
    assert len(calls) == 2


# --- bypasses ----------------------------------------------------------------


def test_ttl_zero_bypasses(tmp_path):
    c = Cache(tmp_path / "c.db")
    calls: list[int] = []
    for _ in range(2):
        c.fetch("tmdb.chart", "k", ttl=0, loader=lambda: calls.append(1) or {"x": 1})
    assert len(calls) == 2  # never cached
    assert c.namespaces() == {}  # nothing stored


def test_refresh_forces_miss(tmp_path):
    p = tmp_path / "c.db"
    Cache(p, ttls={"tmdb.record": 100}).fetch(
        "tmdb.record", "k", ttl=100, loader=lambda: {"x": 1}
    )
    cr = Cache(p, ttls={"tmdb.record": 100}, refresh=True)
    calls: list[int] = []
    cr.fetch("tmdb.record", "k", ttl=100, loader=lambda: calls.append(1) or {"x": 2})
    assert len(calls) == 1  # reloaded despite a fresh row


# --- jitter ------------------------------------------------------------------


def test_jitter_back_dates_created_at(tmp_path, monkeypatch):
    clock = _Clock(1000.0)
    monkeypatch.setattr(cache_mod.time, "time", clock)
    monkeypatch.setattr(cache_mod.random, "uniform", lambda a, b: 0.1)  # 10% of ttl
    c = Cache(tmp_path / "c.db", ttls={"tmdb.record": 100})
    c.fetch("tmdb.record", "k", ttl=100, loader=lambda: {"x": 1})
    row = c._get("tmdb.record", "k")
    assert row is not None
    assert row.created_at == pytest.approx(1000.0 - 0.1 * 100)  # 990.0


# --- resilience --------------------------------------------------------------


def test_corrupt_file_recovers(tmp_path):
    p = tmp_path / "c.db"
    p.write_bytes(b"this is not a sqlite database")
    c = Cache(p, ttls={"tmdb.record": 100})
    calls: list[int] = []
    v = c.fetch("tmdb.record", "k", ttl=100, loader=lambda: calls.append(1) or {"x": 1})
    assert v == {"x": 1}
    c.fetch("tmdb.record", "k", ttl=100, loader=lambda: calls.append(1) or {"x": 2})
    assert len(calls) == 1  # recreated db now caches normally


def test_corrupt_payload_refetches(tmp_path):
    c = Cache(tmp_path / "c.db", ttls={"tmdb.record": 100})
    c.fetch("tmdb.record", "k", ttl=100, loader=lambda: {"x": 1})
    conn = sqlite3.connect(c.path)
    conn.execute("UPDATE cache SET payload = 'not json'")
    conn.commit()
    conn.close()
    calls: list[int] = []
    v = c.fetch("tmdb.record", "k", ttl=100, loader=lambda: calls.append(1) or {"x": 2})
    assert v == {"x": 2}
    assert len(calls) == 1  # undecodable payload -> treated as a miss


# --- versioning --------------------------------------------------------------


def test_version_bump_purges_only_that_namespace(tmp_path, monkeypatch):
    p = tmp_path / "c.db"
    c1 = Cache(p, ttls={"tmdb.record": 100, "tvdb.record": 100})
    c1.fetch("tmdb.record", "a", ttl=100, loader=lambda: {"x": 1})
    c1.fetch("tvdb.record", "b", ttl=100, loader=lambda: {"y": 1})

    monkeypatch.setitem(cache_mod._VERSIONS, "tmdb.record", 2)
    c2 = Cache(p, ttls={"tmdb.record": 100, "tvdb.record": 100})
    calls: list[str] = []
    c2.fetch(
        "tmdb.record", "a", ttl=100, loader=lambda: calls.append("tmdb") or {"x": 9}
    )
    c2.fetch(
        "tvdb.record", "b", ttl=100, loader=lambda: calls.append("tvdb") or {"y": 9}
    )
    assert calls == [
        "tmdb"
    ]  # tmdb.record purged (reloaded); tvdb.record survived (hit)


# --- raw read/write (Arr negative cache) ----------------------------------------------


def test_read_write_raw(tmp_path):
    c = Cache(
        tmp_path / "c.db",
        ttls={
            "radarr.lookup": cache_mod.LOOKUP_TTL,
            "radarr.lookup_empty": cache_mod.LOOKUP_EMPTY_TTL,
        },
    )
    assert c.read("radarr.lookup", "tmdb:1") is None
    c.write("radarr.lookup", "tmdb:1", '{"title": "X"}')
    assert c.read("radarr.lookup", "tmdb:1") == '{"title": "X"}'


def test_raw_write_noop_when_ttl_zero(tmp_path):
    c = Cache(tmp_path / "c.db", ttls={"radarr.lookup": 0.0})
    c.write("radarr.lookup", "k", "v")
    assert c.read("radarr.lookup", "k") is None


# --- maintenance ---------------------------------------------------------


def test_namespaces_prune_and_purge(tmp_path, monkeypatch):
    _no_jitter(monkeypatch)
    clock = _Clock(1000.0)
    monkeypatch.setattr(cache_mod.time, "time", clock)
    c = Cache(tmp_path / "c.db", ttls={"tmdb.record": 100, "tmdb.query": 50})
    c.fetch("tmdb.record", "a", ttl=100, loader=lambda: {"x": 1})
    c.fetch("tmdb.query", "b", ttl=50, loader=lambda: {"y": 1})
    assert c.namespaces() == {"tmdb.record": 1, "tmdb.query": 1}

    clock.t = 1075  # query (ttl 50) now expired; record (ttl 100) still fresh
    assert c.prune_expired() == 1
    assert c.namespaces() == {"tmdb.record": 1}

    c.purge("tmdb.record")
    assert c.namespaces() == {}


def test_cache_cli_info_prune_clear(tmp_path, monkeypatch):
    from pathlib import Path

    from nalanda.__main__ import _cache_command
    from nalanda.config import CacheSettings, Secrets

    monkeypatch.setenv("NALANDA_CONFIG", str(tmp_path / "config.yml"))
    secrets = Secrets(_env_file=None)
    ttls = ttl_map(CacheSettings())
    seeded = Cache(secrets.nalanda_cache, ttls=ttls)
    seeded.fetch("tmdb.record", "k", ttl=ttls["tmdb.record"], loader=lambda: {"x": 1})

    assert _cache_command(secrets, ["info"]) == 0
    assert _cache_command(secrets, ["prune"]) == 0
    assert _cache_command(secrets, ["clear", "tmdb.record"]) == 0
    assert (
        Cache(secrets.nalanda_cache, ttls=ttls).namespaces() == {}
    )  # namespace purged
    assert _cache_command(secrets, ["clear"]) == 0  # whole file
    assert not Path(secrets.nalanda_cache).exists()
    assert _cache_command(secrets, ["bogus"]) == 1  # unknown subcommand
