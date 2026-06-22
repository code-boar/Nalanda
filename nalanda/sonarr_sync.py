"""Declarative Sonarr reconciliation -- the TV analogue of :mod:`nalanda.radarr_sync`.

Same plan->apply split and identity-tag design: the identity tag
(``<tag_prefix><slug>``) tracks DESIRED membership; it's applied on add and (with
``add_existing``) if-exists, and a departing show follows the three-state stale policy.
Shows are matched **tvdb-first**, then tmdb/imdb (Sonarr series carry all three).

What TV adds over movies is **monitoring depth**: a series-level ``monitored`` flag
plus the ``monitor`` strategy (which episodes/seasons a show monitors). The strategy is
applied at add via ``addOptions.monitor``; with ``monitor_existing`` the deterministic
strategies
(all / none / first_season / latest_season) are reconciled **declaratively** -- the
desired per-season ``monitored`` set is diffed against Sonarr and written only on a
difference. The dynamic strategies (future / missing / existing / pilot) are
state-dependent and left to Sonarr after add (monitor_existing then reconciles only the
series-level flag).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ._servarr_sync import (
    BasePlan,
    apply_adds,
    apply_tag_writes,
    finalize_reconcile,
    prepare_reconcile,
    reconcile_existing,
    resolve_named_id,
    sweep,
)
from .clients.sonarr import SonarrClient, index_series_by_ids
from .config import CollectionDef, GlobalSettings, ResolvedSonarr, effective_sonarr
from .logging import get_logger
from .models import MediaItem, SonarrSeries
from .tagging import build_identity_tag

log = get_logger(__name__)

_DETERMINISTIC = frozenset({"all", "none", "first_season", "latest_season"})


def identity_tag_label(name: str, opts: ResolvedSonarr) -> str:
    """A collection's identity tag label: ``<tag_prefix><slug-or-override>``."""
    return build_identity_tag(name, tag_prefix=opts.tag_prefix, tag=opts.tag)


def _lookup_term(item: MediaItem) -> str | None:
    """A Sonarr lookup term for a show, preferring tvdb (native), then tmdb, then
    imdb."""
    if item.tvdb_id is not None:
        return f"tvdb:{item.tvdb_id}"
    if item.tmdb_id is not None:
        return f"tmdb:{item.tmdb_id}"
    if item.imdb_id:
        return f"imdb:{item.imdb_id}"
    return None


def match_series(
    item: MediaItem, index: dict[str, dict[Any, SonarrSeries]]
) -> SonarrSeries | None:
    """Find the Sonarr series for a desired show: tvdb id, then tmdb, then imdb."""
    if item.tvdb_id is not None and item.tvdb_id in index["tvdb"]:
        return index["tvdb"][item.tvdb_id]
    if item.tmdb_id is not None and item.tmdb_id in index["tmdb"]:
        return index["tmdb"][item.tmdb_id]
    if item.imdb_id and item.imdb_id in index["imdb"]:
        return index["imdb"][item.imdb_id]
    return None


def _desired_seasons(series: SonarrSeries, strategy: str) -> dict[int, bool] | None:
    """Desired ``{season_number: monitored}`` for a deterministic strategy, else
    ``None``."""
    if strategy not in _DETERMINISTIC:
        return None
    numbers = [s.season_number for s in series.seasons]
    nonzero = [n for n in numbers if n > 0]
    if strategy == "all":
        target = set(nonzero)
    elif strategy == "none":
        target = set()
    elif strategy == "first_season":
        target = {min(nonzero)} if nonzero else set()
    else:  # latest_season
        target = {max(nonzero)} if nonzero else set()
    return {n: (n in target) for n in numbers}


@dataclass
class SonarrPlan(BasePlan):
    """The Sonarr writes for one collection (``adds`` are lookup terms); adds season
    sync."""

    season_updates: list[tuple[int, dict[int, bool]]] = field(default_factory=list)

    def _parts(self) -> list[str]:
        parts = super()._parts()
        if self.season_updates:
            parts.append(f"seasons~{len(self.season_updates)}")
        return parts


def plan_sonarr(
    *,
    name: str,
    desired_shows: list[MediaItem],
    series_index: dict[str, dict[Any, SonarrSeries]],
    opts: ResolvedSonarr,
    identity_tag_id: int,
    stale_tag_id: int | None,
    quality_profile_id: int | None = None,
) -> SonarrPlan:
    """Diff a collection's desired shows against Sonarr. Pure -- no I/O.

    The departed scan flattens the multi-keyed index and dedupes by series id (a series
    appears under each id space it has).
    """
    all_series = {
        s.id: s for d in series_index.values() for s in d.values() if s.id is not None
    }
    present, fields = reconcile_existing(
        name=name,
        desired=desired_shows,
        matcher=lambda show: match_series(show, series_index),
        universe=all_series.values(),
        add_token=_lookup_term,
        opts=opts,
        identity_tag_id=identity_tag_id,
        stale_tag_id=stale_tag_id,
        quality_profile_id=quality_profile_id,
        monitored_value=opts.monitored,
    )
    plan = SonarrPlan(**fields)
    if opts.monitor_existing:
        for sid, s in present.items():
            desired = _desired_seasons(s, opts.monitor)
            if desired is None:
                continue  # dynamic strategy -> Sonarr owns season monitoring
            current = {se.season_number: se.monitored for se in s.seasons}
            if any(current.get(n) != m for n, m in desired.items()):
                plan.season_updates.append((sid, desired))
    return plan


def apply_sonarr(
    sonarr: SonarrClient,
    plan: SonarrPlan,
    *,
    opts: ResolvedSonarr,
    identity_tag_id: int,
    stale_tag_id: int | None,
    quality_profile_id: int | None,
    language_profile_id: int | None,
    dry_run: bool,
) -> None:
    """Execute a :class:`SonarrPlan`. With ``dry_run`` it only logs the intended
    writes."""
    if plan.up_to_date:
        return
    if dry_run:
        log.info("    [dry-run] Sonarr %s -> %s", plan.name, plan.describe())
        if plan.adds:
            log.info("        would add: %s", plan.adds)
        return
    if plan.adds:
        root_folder = opts.root_folder
        if quality_profile_id is None or not root_folder:
            log.error(
                "    Sonarr %s: add requested but no quality profile / root folder",
                plan.name,
            )
        else:
            apply_adds(
                plan.adds,
                build_payload=lambda term: sonarr.build_add_payload(
                    term,
                    quality_profile_id=quality_profile_id,
                    root_folder=root_folder,
                    language_profile_id=language_profile_id,
                    monitored=opts.monitored,
                    monitor=opts.monitor,
                    series_type=opts.series_type,
                    season_folder=opts.season_folder,
                    tag_ids=[identity_tag_id],
                    search=opts.search,
                    cutoff_search=opts.cutoff_search,
                ),
                failed_recently=sonarr.add_failed_recently,
                mark_failed=sonarr.mark_add_failed,
                bulk_add=sonarr.add_series_bulk,
                added_key=lambda s: s.tvdb_id,
                payload_key=lambda token, payload: payload.get("tvdbId"),
                noun="series",
            )
    apply_tag_writes(
        sonarr.edit_series,
        plan,
        identity_tag_id=identity_tag_id,
        stale_tag_id=stale_tag_id,
        quality_profile_id=quality_profile_id,
        monitored_value=opts.monitored,
    )
    for sid, desired in plan.season_updates:
        sonarr.set_seasons(sid, desired)


def sweep_identity_tag(
    sonarr: SonarrClient,
    *,
    identity_label: str,
    stale_label: str,
    policy: str,
    dry_run: bool,
) -> None:
    """Apply the stale policy to ALL holders of a collection's identity tag."""
    sweep(
        sonarr,
        list_attr="get_series",
        edit_attr="edit_series",
        service="Sonarr",
        noun="show(s)",
        identity_label=identity_label,
        stale_label=stale_label,
        policy=policy,
        dry_run=dry_run,
    )


@dataclass
class SonarrContext:
    """Everything fetched once per run to reconcile every Sonarr-managed collection."""

    index: dict[str, dict[Any, SonarrSeries]]
    profile_id_by_name: dict[str, int]
    profile_ids: set[int]
    lang_id_by_name: dict[str, int]
    lang_ids: set[int]
    root_folder_paths: set[str]
    tag_id_by_label: dict[str, int]


def prime_sonarr_context(sonarr: SonarrClient) -> SonarrContext:
    """Fetch the series index, profiles, language profiles, root folders, and tags."""
    series = sonarr.get_series()
    profiles = sonarr.get_quality_profiles()
    langs = sonarr.get_language_profiles()
    ctx = SonarrContext(
        index=index_series_by_ids(series),
        profile_id_by_name={p.name.casefold(): p.id for p in profiles},
        profile_ids={p.id for p in profiles},
        lang_id_by_name={p.name.casefold(): p.id for p in langs},
        lang_ids={p.id for p in langs},
        root_folder_paths={rf.path for rf in sonarr.get_root_folders()},
        tag_id_by_label={t.label.casefold(): t.id for t in sonarr.get_tags()},
    )
    log.info(
        "Sonarr: %d series indexed, %d profiles, %d root folders.",
        len(series),
        len(ctx.profile_ids),
        len(ctx.root_folder_paths),
    )
    return ctx


def reconcile_sonarr(
    sonarr: SonarrClient,
    coll: CollectionDef,
    name: str,
    *,
    settings: GlobalSettings,
    ctx: SonarrContext,
    desired_shows: list[MediaItem],
    dry_run: bool,
) -> None:
    """Resolve options, validate, plan, and apply Sonarr for one collection."""
    opts = effective_sonarr(coll, settings)
    qp_id, identity_tag_id, stale_label, stale_tag_id = prepare_reconcile(
        sonarr, name, opts=opts, ctx=ctx, service="Sonarr", dry_run=dry_run
    )
    # Language profile is Sonarr v3 only (v4 dropped it -> resolves to None).
    lang_id = resolve_named_id(opts.language_profile, ctx.lang_ids, ctx.lang_id_by_name)
    plan = plan_sonarr(
        name=name,
        desired_shows=desired_shows,
        series_index=ctx.index,
        opts=opts,
        identity_tag_id=identity_tag_id,
        stale_tag_id=stale_tag_id,
        quality_profile_id=qp_id,
    )
    stale_tag_id = finalize_reconcile(
        sonarr,
        name,
        plan,
        opts=opts,
        stale_label=stale_label,
        stale_tag_id=stale_tag_id,
        service="Sonarr",
        dry_run=dry_run,
    )
    apply_sonarr(
        sonarr,
        plan,
        opts=opts,
        identity_tag_id=identity_tag_id,
        stale_tag_id=stale_tag_id,
        quality_profile_id=qp_id,
        language_profile_id=lang_id,
        dry_run=dry_run,
    )
