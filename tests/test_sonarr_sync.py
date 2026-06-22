"""Offline tests for the declarative Sonarr reconciliation
(plan_sonarr / apply_sonarr)."""

from __future__ import annotations

from nalanda.clients.sonarr import index_series_by_ids
from nalanda.config import ResolvedSonarr
from nalanda.models import MediaItem, SonarrSeason, SonarrSeries, SonarrTag
from nalanda.sonarr_sync import (
    SonarrPlan,
    apply_sonarr,
    plan_sonarr,
    sweep_identity_tag,
)


def test_reconcile_sonarr_plans_and_applies_via_fake_client():
    from nalanda.config import CollectionDef, GlobalSettings
    from nalanda.sonarr_sync import SonarrContext, reconcile_sonarr

    class _FakeSonarr:
        def __init__(self):
            self.edits: list = []

        def ensure_tag(self, label):
            return 7

        def edit_series(self, ids, **kw):
            self.edits.append((ids, kw))

    fake = _FakeSonarr()
    coll = CollectionDef(
        media="tv", tvdb_show=1, sonarr={"enable": True, "add_existing": True}
    )
    # Build a series index with one present series (tvdb_id=1, sonarr id=10, untagged)
    series = SonarrSeries(id=10, tvdb_id=1, title="S")
    idx = index_series_by_ids([series])
    ctx = SonarrContext(
        index=idx,
        profile_id_by_name={},
        profile_ids=set(),
        lang_id_by_name={},
        lang_ids=set(),
        root_folder_paths=set(),
        tag_id_by_label={},
    )
    reconcile_sonarr(
        fake,
        coll,
        "C",
        settings=GlobalSettings(),
        ctx=ctx,
        desired_shows=[MediaItem(title="S", tvdb_id=1, media_type="tv")],
        dry_run=False,
    )
    # present + untagged + add_existing -> adopt: one edit_series(add identity tag)
    assert fake.edits == [([10], {"tags": [7], "apply_tags": "add"})]


def opts(**over) -> ResolvedSonarr:
    base = dict(
        add_missing=False,
        add_existing=False,
        upgrade_existing=False,
        monitor_existing=False,
        monitored=True,
        monitor="all",
        search=False,
        cutoff_search=False,
        series_type="standard",
        season_folder=True,
        quality_profile=None,
        language_profile=None,
        root_folder=None,
        tag_prefix="nalanda-",
        stale_tags="mark",
        stale_suffix="-stale",
        tag=None,
    )
    base.update(over)
    return ResolvedSonarr(**base)


def index(*series: SonarrSeries):
    return index_series_by_ids(list(series))


def _desired(*tvdb_ids: int) -> list[MediaItem]:
    return [MediaItem(title=f"S{t}", tvdb_id=t, media_type="tv") for t in tvdb_ids]


def _seasons(*pairs: tuple[int, bool]) -> list[SonarrSeason]:
    return [SonarrSeason(season_number=n, monitored=m) for n, m in pairs]


# ---------------------------------------------------------------- adds / matching


def test_add_missing_uses_tvdb_term():
    idx = index(SonarrSeries(id=10, tvdb_id=1, title="A"))
    plan = plan_sonarr(
        name="C",
        desired_shows=_desired(1, 2),
        series_index=idx,
        opts=opts(
            add_missing=True, add_existing=True, quality_profile=1, root_folder="/tv"
        ),
        identity_tag_id=7,
        stale_tag_id=8,
    )
    assert plan.adds == ["tvdb:2"]  # 1 present, 2 missing
    assert plan.tag_add == [10]  # present show adopted (add_existing)


def test_match_falls_back_to_tmdb():
    # desired show known only by tmdb id still matches a Sonarr series carrying tmdb
    idx = index(SonarrSeries(id=10, tvdb_id=1, tmdb_id=1396, title="A"))
    plan = plan_sonarr(
        name="C",
        desired_shows=[MediaItem(title="BB", tmdb_id=1396, media_type="tv")],
        series_index=idx,
        opts=opts(add_existing=True),
        identity_tag_id=7,
        stale_tag_id=8,
    )
    assert plan.tag_add == [10] and plan.adds == []


# ---------------------------------------------------------------- adoption gating


def test_existing_untagged_not_adopted_without_add_existing():
    idx = index(SonarrSeries(id=10, tvdb_id=1, title="A", tags=[]))
    plan = plan_sonarr(
        name="C",
        desired_shows=_desired(1),
        series_index=idx,
        opts=opts(add_existing=False),
        identity_tag_id=7,
        stale_tag_id=8,
    )
    assert plan.up_to_date


def test_already_tagged_is_idempotent():
    idx = index(SonarrSeries(id=10, tvdb_id=1, title="A", tags=[7]))
    plan = plan_sonarr(
        name="C",
        desired_shows=_desired(1),
        series_index=idx,
        opts=opts(add_existing=True),
        identity_tag_id=7,
        stale_tag_id=8,
    )
    assert plan.up_to_date


# ---------------------------------------------------------------- departures / rejoin


def test_departed_mark_swaps_to_stale():
    idx = index(SonarrSeries(id=20, tvdb_id=9, title="Gone", tags=[7]))
    plan = plan_sonarr(
        name="C",
        desired_shows=[],
        series_index=idx,
        opts=opts(stale_tags="mark"),
        identity_tag_id=7,
        stale_tag_id=8,
    )
    assert plan.tag_remove == [20] and plan.stale_add == [20]


def test_departed_delete_removes_identity_only():
    idx = index(SonarrSeries(id=20, tvdb_id=9, title="Gone", tags=[7]))
    plan = plan_sonarr(
        name="C",
        desired_shows=[],
        series_index=idx,
        opts=opts(stale_tags="delete"),
        identity_tag_id=7,
        stale_tag_id=None,
    )
    assert plan.tag_remove == [20] and plan.stale_add == []


def test_rejoin_clears_stale_and_restores_identity():
    idx = index(SonarrSeries(id=30, tvdb_id=5, title="Back", tags=[8]))
    plan = plan_sonarr(
        name="C",
        desired_shows=_desired(5),
        series_index=idx,
        opts=opts(add_existing=False),
        identity_tag_id=7,
        stale_tag_id=8,
    )
    assert plan.stale_remove == [30] and plan.tag_add == [30]


# ---------------------------------------------------------------- upgrade / monitor


def test_upgrade_existing_only_changes_differing_profiles():
    idx = index(
        SonarrSeries(id=10, tvdb_id=1, title="A", tags=[7], quality_profile_id=2),
        SonarrSeries(id=11, tvdb_id=2, title="B", tags=[7], quality_profile_id=5),
    )
    plan = plan_sonarr(
        name="C",
        desired_shows=_desired(1, 2),
        series_index=idx,
        opts=opts(add_existing=True, upgrade_existing=True, quality_profile=5),
        identity_tag_id=7,
        stale_tag_id=8,
        quality_profile_id=5,
    )
    assert plan.profile_updates == [10]  # A differs; B already on 5


def test_monitor_existing_reconciles_series_flag():
    idx = index(SonarrSeries(id=10, tvdb_id=1, title="A", tags=[7], monitored=False))
    plan = plan_sonarr(
        name="C",
        desired_shows=_desired(1),
        series_index=idx,
        opts=opts(add_existing=True, monitor_existing=True, monitored=True),
        identity_tag_id=7,
        stale_tag_id=8,
    )
    assert plan.monitor_updates == [10]


# ---------------------------------------------------------------- season reconciliation


def test_season_reconcile_all_strategy_diff_first():
    # strategy "all" -> every non-special season monitored; specials (0) off
    series = SonarrSeries(
        id=10,
        tvdb_id=1,
        title="A",
        tags=[7],
        seasons=_seasons((0, True), (1, False), (2, True)),
    )
    plan = plan_sonarr(
        name="C",
        desired_shows=_desired(1),
        series_index=index(series),
        opts=opts(add_existing=True, monitor_existing=True, monitor="all"),
        identity_tag_id=7,
        stale_tag_id=8,
    )
    assert plan.season_updates == [(10, {0: False, 1: True, 2: True})]


def test_season_reconcile_noop_when_already_matching():
    series = SonarrSeries(
        id=10,
        tvdb_id=1,
        title="A",
        tags=[7],
        monitored=True,
        seasons=_seasons((0, False), (1, True), (2, True)),
    )
    plan = plan_sonarr(
        name="C",
        desired_shows=_desired(1),
        series_index=index(series),
        opts=opts(add_existing=True, monitor_existing=True, monitor="all"),
        identity_tag_id=7,
        stale_tag_id=8,
    )
    assert plan.season_updates == []


def test_latest_season_only_monitors_highest():
    series = SonarrSeries(
        id=10,
        tvdb_id=1,
        title="A",
        tags=[7],
        seasons=_seasons((1, True), (2, True), (3, False)),
    )
    plan = plan_sonarr(
        name="C",
        desired_shows=_desired(1),
        series_index=index(series),
        opts=opts(add_existing=True, monitor_existing=True, monitor="latest_season"),
        identity_tag_id=7,
        stale_tag_id=8,
    )
    assert plan.season_updates == [(10, {1: False, 2: False, 3: True})]


def test_dynamic_strategy_skips_season_reconcile():
    series = SonarrSeries(
        id=10,
        tvdb_id=1,
        title="A",
        tags=[7],
        seasons=_seasons((1, False), (2, False)),
    )
    plan = plan_sonarr(
        name="C",
        desired_shows=_desired(1),
        series_index=index(series),
        opts=opts(add_existing=True, monitor_existing=True, monitor="missing"),
        identity_tag_id=7,
        stale_tag_id=8,
    )
    assert plan.season_updates == []  # Sonarr owns dynamic strategies


# ---------------------------------------------------------------- apply


def test_apply_dry_run_writes_nothing():
    class Boom:
        def __getattr__(self, _):
            def fail(*a, **k):
                raise AssertionError("write attempted during dry-run")

            return fail

    plan = SonarrPlan(
        name="C", adds=["tvdb:1"], tag_add=[2], season_updates=[(3, {1: True})]
    )
    apply_sonarr(
        Boom(),
        plan,
        opts=opts(root_folder="/tv"),
        identity_tag_id=7,
        stale_tag_id=8,
        quality_profile_id=1,
        language_profile_id=1,
        dry_run=True,
    )


def test_apply_live_issues_expected_writes():
    calls: list = []

    class Rec:
        def add_failed_recently(self, term):
            return False

        def build_add_payload(self, term, **kw):
            calls.append(("build", term, kw))
            return {"tvdbId": 1, **kw}

        def add_series_bulk(self, payloads):
            calls.append(("add_bulk", [p["tvdbId"] for p in payloads]))
            return [
                SonarrSeries(id=99, tvdb_id=p["tvdbId"], title="x") for p in payloads
            ]

        def mark_add_failed(self, term):
            calls.append(("mark_failed", term))

        def edit_series(self, ids, **kw):
            calls.append(("edit", list(ids), kw))

        def set_seasons(self, sid, desired):
            calls.append(("seasons", sid, desired))

    plan = SonarrPlan(
        name="C",
        adds=["tvdb:1"],
        tag_add=[2],
        tag_remove=[3],
        stale_add=[4],
        stale_remove=[5],
        profile_updates=[6],
        monitor_updates=[7],
        season_updates=[(8, {1: True})],
    )
    apply_sonarr(
        Rec(),
        plan,
        opts=opts(
            root_folder="/tv",
            monitored=True,
            monitor="first_season",
            search=True,
            series_type="anime",
            season_folder=False,
            cutoff_search=True,
        ),
        identity_tag_id=10,
        stale_tag_id=20,
        quality_profile_id=4,
        language_profile_id=1,
        dry_run=False,
    )
    build = next(c for c in calls if c[0] == "build")
    assert build[1] == "tvdb:1"
    assert build[2]["monitor"] == "first_season" and build[2]["series_type"] == "anime"
    assert build[2]["tag_ids"] == [10] and build[2]["language_profile_id"] == 1
    assert ("add_bulk", [1]) in calls  # one batched import
    assert not any(c[0] == "mark_failed" for c in calls)
    assert ("edit", [2], {"tags": [10], "apply_tags": "add"}) in calls
    assert ("edit", [3], {"tags": [10], "apply_tags": "remove"}) in calls
    assert ("edit", [4], {"tags": [20], "apply_tags": "add"}) in calls
    assert ("edit", [5], {"tags": [20], "apply_tags": "remove"}) in calls
    assert ("edit", [6], {"quality_profile_id": 4}) in calls
    assert ("edit", [7], {"monitored": True}) in calls
    assert ("seasons", 8, {1: True}) in calls


# ---------------------------------------------------------------- sweep


def test_sweep_identity_tag_mark():
    calls: list = []

    class Rec:
        def get_tags(self):
            return [SonarrTag(id=10, label="nalanda-x")]

        def get_series(self):
            return [
                SonarrSeries(id=1, tvdb_id=1, title="a", tags=[10]),
                SonarrSeries(id=2, tvdb_id=2, title="b", tags=[]),
            ]

        def edit_series(self, ids, **kw):
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
