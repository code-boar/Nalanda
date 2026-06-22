"""Offline tests for the declarative Radarr reconciliation
(plan_radarr / apply_radarr)."""

from __future__ import annotations

from nalanda.config import ResolvedRadarr
from nalanda.models import MediaItem, RadarrMovie, RadarrTag
from nalanda.radarr_sync import (
    RadarrPlan,
    apply_radarr,
    plan_radarr,
    sweep_identity_tag,
)


def test_reconcile_radarr_plans_and_applies_via_fake_client():
    from nalanda.config import CollectionDef, GlobalSettings
    from nalanda.radarr_sync import RadarrContext, reconcile_radarr

    class _FakeRadarr:
        def __init__(self):
            self.edits: list = []

        def ensure_tag(self, label):
            return 7

        def edit_movies(self, ids, **kw):
            self.edits.append((ids, kw))

    fake = _FakeRadarr()
    coll = CollectionDef(
        media="movie", tmdb_collection=1, radarr={"enable": True, "add_existing": True}
    )
    ctx = RadarrContext(
        index={1: RadarrMovie(id=10, tmdb_id=1, title="A")},
        profile_id_by_name={},
        profile_ids=set(),
        root_folder_paths=set(),
        tag_id_by_label={},
    )
    reconcile_radarr(
        fake,
        coll,
        "C",
        settings=GlobalSettings(),
        ctx=ctx,
        desired_movies=[MediaItem(title="A", tmdb_id=1)],
        dry_run=False,
    )
    # present + untagged + add_existing -> adopt: one edit_movies(add identity tag)
    assert fake.edits == [([10], {"tags": [7], "apply_tags": "add"})]


def opts(**over) -> ResolvedRadarr:
    base = dict(
        add_missing=False,
        add_existing=False,
        upgrade_existing=False,
        monitor_existing=False,
        monitored=True,
        search=False,
        minimum_availability="released",
        quality_profile=None,
        root_folder=None,
        tag_prefix="nalanda-",
        stale_tags="mark",
        stale_suffix="-stale",
        tag=None,
    )
    base.update(over)
    return ResolvedRadarr(**base)


def index(*movies: RadarrMovie) -> dict[int, RadarrMovie]:
    return {m.tmdb_id: m for m in movies if m.tmdb_id is not None}


def _desired(*tmdb_ids: int) -> list[MediaItem]:
    return [MediaItem(title=f"M{t}", tmdb_id=t) for t in tmdb_ids]


# ---------------------------------------------------------------- adds


def test_add_missing_lists_absent_desired():
    idx = index(RadarrMovie(id=10, tmdb_id=1, title="A"))
    plan = plan_radarr(
        name="C",
        desired_movies=_desired(1, 2),
        radarr_index=idx,
        opts=opts(
            add_missing=True, add_existing=True, quality_profile=1, root_folder="/m"
        ),
        identity_tag_id=7,
        stale_tag_id=8,
    )
    assert plan.adds == [2]  # 1 present, 2 missing
    assert plan.tag_add == [10]  # present movie adopted (add_existing)


def test_no_adds_when_add_missing_off():
    idx = index(RadarrMovie(id=10, tmdb_id=1, title="A", tags=[7]))
    plan = plan_radarr(
        name="C",
        desired_movies=_desired(1, 2),
        radarr_index=idx,
        opts=opts(add_existing=True),
        identity_tag_id=7,
        stale_tag_id=8,
    )
    assert plan.adds == []


# ---------------------------------------------------------------- adoption gating


def test_existing_untagged_not_adopted_without_add_existing():
    idx = index(RadarrMovie(id=10, tmdb_id=1, title="A", tags=[]))
    plan = plan_radarr(
        name="C",
        desired_movies=_desired(1),
        radarr_index=idx,
        opts=opts(add_existing=False),
        identity_tag_id=7,
        stale_tag_id=8,
    )
    assert plan.tag_add == []
    assert plan.up_to_date


def test_existing_untagged_adopted_with_add_existing():
    idx = index(RadarrMovie(id=10, tmdb_id=1, title="A", tags=[]))
    plan = plan_radarr(
        name="C",
        desired_movies=_desired(1),
        radarr_index=idx,
        opts=opts(add_existing=True),
        identity_tag_id=7,
        stale_tag_id=8,
    )
    assert plan.tag_add == [10]


def test_already_tagged_is_idempotent():
    idx = index(RadarrMovie(id=10, tmdb_id=1, title="A", tags=[7]))
    plan = plan_radarr(
        name="C",
        desired_movies=_desired(1),
        radarr_index=idx,
        opts=opts(add_existing=True),
        identity_tag_id=7,
        stale_tag_id=8,
    )
    assert plan.up_to_date


# ------------------------------------------------------------ departures (stale policy)


def test_departed_mark_swaps_to_stale():
    idx = index(RadarrMovie(id=20, tmdb_id=9, title="Gone", tags=[7]))
    plan = plan_radarr(
        name="C",
        desired_movies=[],
        radarr_index=idx,
        opts=opts(stale_tags="mark"),
        identity_tag_id=7,
        stale_tag_id=8,
    )
    assert plan.tag_remove == [20]
    assert plan.stale_add == [20]


def test_departed_mark_when_stale_tag_not_yet_created():
    # First-ever mark: the stale tag doesn't exist (stale_tag_id=None). It must
    # still be planned (the caller creates the tag lazily), not silently degrade
    # to a plain remove.
    idx = index(RadarrMovie(id=20, tmdb_id=9, title="Gone", tags=[7]))
    plan = plan_radarr(
        name="C",
        desired_movies=[],
        radarr_index=idx,
        opts=opts(stale_tags="mark"),
        identity_tag_id=7,
        stale_tag_id=None,
    )
    assert plan.tag_remove == [20]
    assert plan.stale_add == [20]


def test_departed_delete_removes_identity_only():
    idx = index(RadarrMovie(id=20, tmdb_id=9, title="Gone", tags=[7]))
    plan = plan_radarr(
        name="C",
        desired_movies=[],
        radarr_index=idx,
        opts=opts(stale_tags="delete"),
        identity_tag_id=7,
        stale_tag_id=None,
    )
    assert plan.tag_remove == [20]
    assert plan.stale_add == []


def test_departed_keep_is_noop():
    idx = index(RadarrMovie(id=20, tmdb_id=9, title="Gone", tags=[7]))
    plan = plan_radarr(
        name="C",
        desired_movies=[],
        radarr_index=idx,
        opts=opts(stale_tags="keep"),
        identity_tag_id=7,
        stale_tag_id=None,
    )
    assert plan.up_to_date


def test_departures_run_even_without_add_existing():
    # We applied the tag previously; cleaning our own tag isn't gated by add_existing.
    idx = index(RadarrMovie(id=20, tmdb_id=9, title="Gone", tags=[7]))
    plan = plan_radarr(
        name="C",
        desired_movies=[],
        radarr_index=idx,
        opts=opts(add_existing=False, stale_tags="mark"),
        identity_tag_id=7,
        stale_tag_id=8,
    )
    assert plan.tag_remove == [20] and plan.stale_add == [20]


# ---------------------------------------------------------------- rejoin


def test_rejoin_clears_stale_and_restores_live_tag():
    idx = index(RadarrMovie(id=30, tmdb_id=5, title="Back", tags=[8]))
    plan = plan_radarr(
        name="C",
        desired_movies=_desired(5),
        radarr_index=idx,
        opts=opts(add_existing=False),  # rejoin works regardless of add_existing
        identity_tag_id=7,
        stale_tag_id=8,
    )
    assert plan.stale_remove == [30]
    assert plan.tag_add == [30]


# ------------------------------------------------------- upgrade / monitor (diff-first)


def test_upgrade_existing_only_changes_differing_profiles():
    idx = index(
        RadarrMovie(id=10, tmdb_id=1, title="A", tags=[7], quality_profile_id=2),
        RadarrMovie(id=11, tmdb_id=2, title="B", tags=[7], quality_profile_id=5),
    )
    plan = plan_radarr(
        name="C",
        desired_movies=_desired(1, 2),
        radarr_index=idx,
        opts=opts(add_existing=True, upgrade_existing=True, quality_profile=5),
        identity_tag_id=7,
        stale_tag_id=8,
        quality_profile_id=5,
    )
    assert plan.profile_updates == [10]  # A differs; B already on 5


def test_monitor_existing_only_changes_differing():
    idx = index(RadarrMovie(id=10, tmdb_id=1, title="A", tags=[7], monitored=False))
    plan = plan_radarr(
        name="C",
        desired_movies=_desired(1),
        radarr_index=idx,
        opts=opts(add_existing=True, monitor_existing=True, monitored=True),
        identity_tag_id=7,
        stale_tag_id=8,
    )
    assert plan.monitor_updates == [10]


# ---------------------------------------------------------------- apply_radarr


def test_apply_dry_run_writes_nothing():
    class Boom:
        def add_movie(self, *a, **k):
            raise AssertionError("write attempted during dry-run")

        def edit_movies(self, *a, **k):
            raise AssertionError("write attempted during dry-run")

    plan = RadarrPlan(name="C", adds=[1], tag_add=[2], tag_remove=[3])
    apply_radarr(
        Boom(),
        plan,
        opts=opts(root_folder="/m"),
        identity_tag_id=7,
        stale_tag_id=8,
        quality_profile_id=1,
        dry_run=True,
    )


def test_apply_live_issues_expected_writes():
    calls: list[tuple] = []

    class Rec:
        def add_failed_recently(self, tmdb_id):
            return False

        def build_add_payload(self, tmdb_id, **kw):
            calls.append(("build", tmdb_id, kw))
            return {"tmdbId": tmdb_id, **kw}

        def add_movies(self, payloads):
            calls.append(("add_movies", [p["tmdbId"] for p in payloads]))
            return [
                RadarrMovie(id=99, tmdb_id=p["tmdbId"], title="x") for p in payloads
            ]

        def mark_add_failed(self, tmdb_id):
            calls.append(("mark_failed", tmdb_id))

        def edit_movies(self, ids, **kw):
            calls.append(("edit", list(ids), kw))

    plan = RadarrPlan(
        name="C",
        adds=[1],
        tag_add=[2],
        tag_remove=[3],
        stale_add=[4],
        stale_remove=[5],
        profile_updates=[6],
        monitor_updates=[7],
    )
    apply_radarr(
        Rec(),
        plan,
        opts=opts(root_folder="/m", monitored=True, search=True),
        identity_tag_id=10,
        stale_tag_id=20,
        quality_profile_id=4,
        dry_run=False,
    )
    assert (
        "build",
        1,
        {
            "quality_profile_id": 4,
            "root_folder": "/m",
            "monitored": True,
            "minimum_availability": "released",
            "tag_ids": [10],
            "search": True,
        },
    ) in calls
    assert ("add_movies", [1]) in calls  # one batched import
    assert not any(
        c[0] == "mark_failed" for c in calls
    )  # add succeeded -> no failure marker
    assert ("edit", [2], {"tags": [10], "apply_tags": "add"}) in calls
    assert ("edit", [3], {"tags": [10], "apply_tags": "remove"}) in calls
    assert ("edit", [4], {"tags": [20], "apply_tags": "add"}) in calls
    assert ("edit", [5], {"tags": [20], "apply_tags": "remove"}) in calls
    assert ("edit", [6], {"quality_profile_id": 4}) in calls
    assert ("edit", [7], {"monitored": True}) in calls


# ---------------------------------------------------------------- sweep_identity_tag


def test_sweep_identity_tag_mark():
    calls: list = []

    class Rec:
        def get_tags(self):
            return [RadarrTag(id=10, label="nalanda-x")]

        def get_movies(self):
            return [
                RadarrMovie(id=1, tmdb_id=1, title="a", tags=[10]),
                RadarrMovie(id=2, tmdb_id=2, title="b", tags=[]),
            ]

        def edit_movies(self, ids, **kw):
            calls.append(("edit", list(ids), kw))

        def ensure_tag(self, label):
            calls.append(("ensure", label))
            return 20

    sweep_identity_tag(
        Rec(),
        identity_label="nalanda-x",
        stale_label="nalanda-x-stale",
        policy="mark",
        dry_run=False,
    )
    assert ("edit", [1], {"tags": [10], "apply_tags": "remove"}) in calls
    assert ("ensure", "nalanda-x-stale") in calls
    assert ("edit", [1], {"tags": [20], "apply_tags": "add"}) in calls


def test_sweep_identity_tag_keep_is_noop():
    class Boom:
        def get_tags(self):
            raise AssertionError("keep policy should not touch Radarr")

    sweep_identity_tag(
        Boom(),
        identity_label="nalanda-x",
        stale_label="nalanda-x-stale",
        policy="keep",
        dry_run=False,
    )
