"""Offline tests for the webhook daemon's pure units (no sockets, no real run)."""

from __future__ import annotations

from datetime import datetime

from nalanda.server import ALL, TriggerCoordinator, next_cron, resolve_scope

T2C = {"nalanda-alpha": "Alpha", "nalanda-beta": "Beta"}
NAMES = {"Alpha", "Beta", "Gamma"}


def rs(body) -> set[str] | None:
    return resolve_scope(body, collection_names=NAMES, tag_to_collection=T2C)


# ------------------------------------------------------------------ resolve_scope


def test_radarr_movie_tags_map_to_collection():
    assert rs(
        {
            "eventType": "Download",
            "movie": {"tags": ["nalanda-alpha", "other-x"]},
        }
    ) == {"Alpha"}


def test_sonarr_series_tags_map_to_collection():
    assert rs({"series": {"tags": ["nalanda-beta"]}}) == {"Beta"}


def test_toplevel_tags_case_insensitive():
    assert rs({"tags": ["Nalanda-Alpha"]}) == {"Alpha"}


def test_multiple_tags_union():
    assert rs({"movie": {"tags": ["nalanda-alpha", "nalanda-beta"]}}) == {
        "Alpha",
        "Beta",
    }


def test_unknown_or_foreign_tag_is_noop():
    assert rs({"movie": {"tags": ["nalanda-unknown", "other-y"]}}) is None
    assert (
        rs({"movie": {"tags": ["nalanda-alpha-stale"]}}) is None
    )  # stale label not mapped


def test_test_event_with_test_tag_is_noop():
    assert rs({"eventType": "Test", "tags": ["test-tag"]}) is None


def test_explicit_collections_validated():
    assert rs({"collections": ["Gamma", "Nope"]}) == {"Gamma"}


def test_empty_or_all_body_is_noop():
    assert rs({}) is None
    assert rs({"all": True}) is None  # /trigger never resolves to a full run


def _build_tag_map(coll, name, settings):
    """Mirror serve()'s tag_to_collection build (gated on `enable`)."""
    from nalanda.config import effective_radarr, effective_sonarr
    from nalanda.radarr_sync import identity_tag_label
    from nalanda.sonarr_sync import identity_tag_label as sonarr_identity_tag_label

    t2c: dict[str, str] = {}
    if coll.radarr is not None and coll.radarr.enable:
        t2c[identity_tag_label(name, effective_radarr(coll, settings)).casefold()] = (
            name
        )
    if coll.sonarr is not None and coll.sonarr.enable:
        t2c[
            sonarr_identity_tag_label(name, effective_sonarr(coll, settings)).casefold()
        ] = name
    return t2c


def test_mixed_collection_scopes_from_both_webhooks():
    # An enabled mixed collection has both a Radarr and a Sonarr identity tag,
    # so EITHER a Radarr movie webhook or a Sonarr series webhook scopes a run
    # to it.
    from nalanda.config import CollectionDef, GlobalSettings

    coll = CollectionDef(
        media="mixed",
        tmdb_collection=10,
        tmdb_show=[1],
        radarr={"enable": True},
        sonarr={"enable": True},
    )
    t2c = _build_tag_map(coll, "Gamma", GlobalSettings())

    names = {"Gamma"}
    via_radarr = resolve_scope(
        {"movie": {"tags": list(t2c)}}, collection_names=names, tag_to_collection=t2c
    )
    via_sonarr = resolve_scope(
        {"series": {"tags": list(t2c)}}, collection_names=names, tag_to_collection=t2c
    )
    assert via_radarr == via_sonarr == {"Gamma"}


def test_disabled_arr_block_is_not_registered_for_scoping():
    # enable unset -> false -> the collection isn't tagged in Radarr, so no webhook
    # can scope to it (serve() skips it in the tag map).
    from nalanda.config import CollectionDef, GlobalSettings

    coll = CollectionDef(
        media="movie", tmdb_collection=1, radarr={}
    )  # enable defaults false
    assert _build_tag_map(coll, "Inert", GlobalSettings()) == {}


# ------------------------------------------------------------------ next_cron


def test_next_cron_is_deterministic():
    assert next_cron("0 4 * * *", datetime(2026, 6, 5, 3, 0)) == datetime(
        2026, 6, 5, 4, 0
    )
    assert next_cron("0 4 * * *", datetime(2026, 6, 5, 5, 0)) == datetime(
        2026, 6, 6, 4, 0
    )


# ------------------------------------------------------------------ TriggerCoordinator


def _coord(*, allow_full=False, debounce=3600.0, kinds=("collections",)):
    """A coordinator whose runners record ``(kind, names, prune)`` per fire."""
    calls: list = []

    def make(kind):
        return lambda names, prune: calls.append((kind, names, prune))

    c = TriggerCoordinator(
        {k: make(k) for k in kinds},
        debounce_seconds=debounce,
        max_wait_seconds=None,
        allow_full_run=allow_full,
    )
    return c, calls


def test_debounce_coalesces_into_one_run_of_the_union():
    c, calls = _coord()
    c.submit("collections", {"A"})
    c.submit("collections", {"B"})
    c.flush()
    assert calls == [("collections", ["A", "B"], False)]


def test_full_run_supersedes_pending_names():
    c, calls = _coord(allow_full=True)
    c.submit("collections", {"A"})
    c.submit("collections", ALL)
    c.flush()
    assert calls == [("collections", None, True)]  # full run always prunes


def test_full_run_gated_when_not_allowed():
    c, calls = _coord(allow_full=False)
    assert c.submit("collections", ALL, gated=True) == (False, 403)
    c.flush()
    assert calls == []  # refused -> nothing queued, nothing ran


def test_scheduler_full_run_bypasses_gate():
    c, calls = _coord(allow_full=False)
    assert c.submit("collections", ALL, gated=False) == (True, 202)
    c.flush()
    assert calls == [("collections", None, True)]


def test_scoped_runs_always_allowed():
    c, calls = _coord(allow_full=False)
    assert c.submit("collections", {"Alpha"}, gated=True) == (True, 202)
    c.flush()
    assert calls == [("collections", ["Alpha"], False)]


def test_distinct_kinds_do_not_coalesce():
    # A metadata job and a collections job stay separate and both fire
    # (sorted kind order).
    c, calls = _coord(kinds=("collections", "metadata"))
    c.submit("collections", {"A"}, gated=False)
    c.submit("metadata", {"A"}, gated=False)
    c.flush()
    assert calls == [
        ("collections", ["A"], False),
        ("metadata", ["A"], False),
    ]


def test_scoped_run_can_request_prune():
    # The scheduler sets prune=True on the default-cron run even when it's scoped.
    c, calls = _coord()
    c.submit("collections", {"A"}, gated=False, prune=True)
    c.flush()
    assert calls == [("collections", ["A"], True)]


def test_prune_is_ord_across_coalesced_submits():
    c, calls = _coord()
    c.submit("collections", {"A"}, gated=False, prune=True)
    c.submit("collections", {"B"}, gated=False, prune=False)
    c.flush()
    assert calls == [("collections", ["A", "B"], True)]


def test_prune_only_empty_scope_still_runs():
    # An empty default-cron group (every collection overrides it) still prunes.
    c, calls = _coord()
    c.submit("collections", set(), gated=False, prune=True)
    c.flush()
    assert calls == [("collections", [], True)]


def test_empty_scope_without_prune_is_noop():
    c, calls = _coord()
    c.submit("collections", set())
    c.flush()
    assert calls == []


def test_triggers_during_a_run_are_drained_by_the_same_fire():
    # A trigger arriving while a run is in progress must be picked up by the same
    # fire loop (re-checking pending), not deferred to a fresh timer -- deferring
    # is what stacks blocked threads under load. With a huge debounce, the
    # follow-up only runs if the loop re-checks.
    calls: list = []

    def runner(names, prune):
        calls.append((names, prune))
        if names == ["A"]:  # simulate a webhook landing mid-run
            coord.submit("collections", {"B"}, gated=False)

    coord = TriggerCoordinator(
        {"collections": runner},
        debounce_seconds=3600.0,
        max_wait_seconds=None,
        allow_full_run=False,
    )
    coord.submit("collections", {"A"}, gated=False)
    coord.flush()
    assert calls == [(["A"], False), (["B"], False)]


# ------------------------------------------------------------------ serve() wiring


def test_serve_wires_one_thread_per_cron(tmp_path, monkeypatch):
    """serve() groups the cascade into per-cron scheduler threads,
    the default marked +prune."""
    import nalanda.__main__ as main
    import nalanda.server as srv
    from nalanda.config import Secrets

    started: list = []

    class FakeThread:  # capture the scheduler thread args without running anything
        def __init__(self, *, target=None, args=(), daemon=None):
            self._args = args

        def start(self):
            started.append(self._args)

    class FakeHTTPD:  # no socket, no blocking -- exit serve() at once
        def __init__(self, addr, handler):
            pass

        def serve_forever(self):
            raise KeyboardInterrupt

        def shutdown(self):
            pass

    monkeypatch.setattr(srv.threading, "Thread", FakeThread)
    monkeypatch.setattr(srv, "ThreadingHTTPServer", FakeHTTPD)
    monkeypatch.setattr(main, "_run", lambda *a, **k: 0)

    (tmp_path / "config.yml").write_text(
        """
settings:
  run_schedules: {hourly: "0 * * * *", daily: "0 4 * * *"}
  run_schedule: daily
  jobs: {collections: daily}
collections:
  A: {media: movie, tmdb_collection: 1, run_schedule: hourly}
  B: {media: movie, tmdb_collection: 2}
  C: {media: movie, tmdb_collection: 3, run_schedule: none}
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WEBHOOK_SECRET", "x")

    assert srv.serve(Secrets(_env_file=None), dry_run=True) == 0

    # args = (cron, kind, scope, is_default, coordinator, stop)
    # serve() also starts a metadata scheduler thread when a metadata cron is
    # configured; this config has no jobs.metadata override so it falls through
    # to run_schedule "daily" -> "0 4 * * *".  Assert that thread started, then
    # verify only the collection threads.
    assert [a[0] for a in started if a[1] == "metadata"] == ["0 4 * * *"]
    collections_threads = [a for a in started if a[1] == "collections"]
    wired = {a[0]: (sorted(a[2]), a[3]) for a in collections_threads}
    assert wired == {
        "0 * * * *": (["A"], False),  # A's hourly override -> scoped
        "0 4 * * *": (["B"], True),  # B inherits the default -> +prune; C opted out
    }


def test_serve_runs_without_webhook_secret(tmp_path, monkeypatch):
    """serve() runs schedules-only (no startup guard) when WEBHOOK_SECRET is unset."""
    import nalanda.__main__ as main
    import nalanda.server as srv
    from nalanda.config import Secrets

    started: list = []

    class FakeThread:
        def __init__(self, *, target=None, args=(), daemon=None):
            self._args = args

        def start(self):
            started.append(self._args)

    class FakeHTTPD:
        def __init__(self, addr, handler):
            pass

        def serve_forever(self):
            raise KeyboardInterrupt

        def shutdown(self):
            pass

    monkeypatch.setattr(srv.threading, "Thread", FakeThread)
    monkeypatch.setattr(srv, "ThreadingHTTPServer", FakeHTTPD)
    monkeypatch.setattr(main, "_run", lambda *a, **k: 0)

    (tmp_path / "config.yml").write_text(
        """
settings:
  run_schedule: "0 4 * * *"
collections:
  A: {media: movie, tmdb_collection: 1}
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)

    assert srv.serve(Secrets(_env_file=None), dry_run=True) == 0
    assert {a[0] for a in started} == {
        "0 4 * * *"
    }  # schedule wired even without a secret


def test_post_returns_503_when_no_secret():
    """No secret configured -> POST routes are disabled (503) before the token check."""
    from types import SimpleNamespace

    import nalanda.server as srv

    h = srv._Handler.__new__(srv._Handler)
    h.server = SimpleNamespace(_ctx=SimpleNamespace(secret=""))
    h.client_address = ("1.2.3.4", 0)
    h.command = "POST"
    h.path = "/trigger"
    sent: list = []
    h._send = lambda status, payload=None: sent.append((status, payload))

    h.do_POST()
    assert sent == [(503, {"error": "webhook disabled"})]


def test_coordinator_routes_metadata_kind():
    # The daemon relies on TriggerCoordinator dispatching a registered "metadata"
    # kind to its own runner (kinds run sequentially under one lock).
    calls: list[tuple] = []
    coord = TriggerCoordinator(
        {
            "collections": lambda n, p: calls.append(("collections", n, p)),
            "metadata": lambda n, p: calls.append(("metadata", n, p)),
        },
        debounce_seconds=0.0,
        max_wait_seconds=None,
        allow_full_run=True,
    )
    coord.submit("metadata", ALL, gated=False)
    coord.flush()
    # ALL sets pending_full, so _fire passes names=None and prune=True (pending_full).
    assert calls == [("metadata", None, True)]
