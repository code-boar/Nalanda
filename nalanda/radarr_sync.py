"""Declarative Radarr reconciliation.

Mirrors the ``reconcile``/``collection`` plan->apply split: :func:`plan_radarr` is a
pure function that diffs a collection's desired members against Radarr's current state
and returns a :class:`RadarrPlan`; :func:`apply_radarr` executes it (or, with
``dry_run``, just logs what it would do).

The identity tag (``<tag_prefix><slug>``) tracks DESIRED membership. Tagging follows the
tag design: applied **on add** (a movie Nalanda creates is tagged at add time) and
**if-exists** (``add_existing`` tags movies already in Radarr) -- always decoupled from
triggering a search. When a movie LEAVES a collection the stale policy decides its fate
(delete / mark with ``-stale`` / keep); a re-joining movie has its stale tag cleared and
the live tag restored. Cleanup of our own tags (departures / rejoin) runs regardless of
``add_existing`` -- that flag only gates *adopting* untagged pre-existing movies.
"""

from __future__ import annotations

from dataclasses import dataclass

from ._servarr_sync import (
    BasePlan,
    apply_adds,
    apply_tag_writes,
    finalize_reconcile,
    prepare_reconcile,
    reconcile_existing,
    sweep,
)
from .clients.radarr import RadarrClient, index_movies_by_tmdb
from .config import CollectionDef, GlobalSettings, ResolvedRadarr, effective_radarr
from .logging import get_logger
from .models import MediaItem, RadarrMovie
from .tagging import build_identity_tag

log = get_logger(__name__)


def identity_tag_label(name: str, opts: ResolvedRadarr) -> str:
    """A collection's identity tag label: ``<tag_prefix><slug-or-override>``."""
    return build_identity_tag(name, tag_prefix=opts.tag_prefix, tag=opts.tag)


@dataclass
class RadarrPlan(BasePlan):
    """The Radarr writes needed to reconcile one collection (``adds`` are TMDB ids)."""


def plan_radarr(
    *,
    name: str,
    desired_movies: list[MediaItem],
    radarr_index: dict[int, RadarrMovie],
    opts: ResolvedRadarr,
    identity_tag_id: int,
    stale_tag_id: int | None,
    quality_profile_id: int | None = None,
) -> RadarrPlan:
    """Diff a collection's desired movies against Radarr. Pure -- no I/O.

    Iterating ``radarr_index.values()`` for the departed scan covers every identity-tag
    holder: we only ever tag movies by tmdb id, so any tagged movie has one and is
    indexed.
    """
    _, fields = reconcile_existing(
        name=name,
        desired=desired_movies,
        matcher=lambda m: (
            radarr_index.get(m.tmdb_id) if m.tmdb_id is not None else None
        ),
        universe=radarr_index.values(),
        add_token=lambda m: m.tmdb_id,
        opts=opts,
        identity_tag_id=identity_tag_id,
        stale_tag_id=stale_tag_id,
        quality_profile_id=quality_profile_id,
        monitored_value=opts.monitored,
    )
    return RadarrPlan(**fields)


def apply_radarr(
    radarr: RadarrClient,
    plan: RadarrPlan,
    *,
    opts: ResolvedRadarr,
    identity_tag_id: int,
    stale_tag_id: int | None,
    quality_profile_id: int | None,
    dry_run: bool,
) -> None:
    """Execute a :class:`RadarrPlan`. With ``dry_run`` it only logs the intended
    writes."""
    if plan.up_to_date:
        return
    if dry_run:
        log.info("    [dry-run] Radarr %s -> %s", plan.name, plan.describe())
        if plan.adds:
            log.info("        would add tmdb ids: %s", plan.adds)
        return
    if plan.adds:
        root_folder = opts.root_folder
        if quality_profile_id is None or not root_folder:
            log.error(
                "    Radarr %s: add requested but no quality profile / root folder",
                plan.name,
            )
        else:
            apply_adds(
                plan.adds,
                build_payload=lambda tid: radarr.build_add_payload(
                    tid,
                    quality_profile_id=quality_profile_id,
                    root_folder=root_folder,
                    monitored=opts.monitored,
                    minimum_availability=opts.minimum_availability,
                    tag_ids=[identity_tag_id],
                    search=opts.search,
                ),
                failed_recently=radarr.add_failed_recently,
                mark_failed=radarr.mark_add_failed,
                bulk_add=radarr.add_movies,
                added_key=lambda m: m.tmdb_id,
                payload_key=lambda token, payload: token,
                noun="tmdb id(s)",
            )
    apply_tag_writes(
        radarr.edit_movies,
        plan,
        identity_tag_id=identity_tag_id,
        stale_tag_id=stale_tag_id,
        quality_profile_id=quality_profile_id,
        monitored_value=opts.monitored,
    )


def sweep_identity_tag(
    radarr: RadarrClient,
    *,
    identity_label: str,
    stale_label: str,
    policy: str,
    dry_run: bool,
) -> None:
    """Apply the stale policy to ALL holders of a collection's identity tag."""
    sweep(
        radarr,
        list_attr="get_movies",
        edit_attr="edit_movies",
        service="Radarr",
        noun="movie(s)",
        identity_label=identity_label,
        stale_label=stale_label,
        policy=policy,
        dry_run=dry_run,
    )


@dataclass
class RadarrContext:
    """Everything fetched once per run to reconcile every Radarr-managed collection."""

    index: dict[int, RadarrMovie]
    profile_id_by_name: dict[str, int]
    profile_ids: set[int]
    root_folder_paths: set[str]
    tag_id_by_label: dict[str, int]


def prime_radarr_context(radarr: RadarrClient) -> RadarrContext:
    """Fetch the movie index, profiles, root folders and tags in one pass."""
    profiles = radarr.get_quality_profiles()
    ctx = RadarrContext(
        index=index_movies_by_tmdb(radarr.get_movies()),
        profile_id_by_name={p.name.casefold(): p.id for p in profiles},
        profile_ids={p.id for p in profiles},
        root_folder_paths={rf.path for rf in radarr.get_root_folders()},
        tag_id_by_label={t.label.casefold(): t.id for t in radarr.get_tags()},
    )
    log.info(
        "Radarr: %d movies indexed, %d profiles, %d root folders.",
        len(ctx.index),
        len(ctx.profile_ids),
        len(ctx.root_folder_paths),
    )
    return ctx


def reconcile_radarr(
    radarr: RadarrClient,
    coll: CollectionDef,
    name: str,
    *,
    settings: GlobalSettings,
    ctx: RadarrContext,
    desired_movies: list[MediaItem],
    dry_run: bool,
) -> None:
    """Resolve options, validate, plan, and apply Radarr for one collection."""
    opts = effective_radarr(coll, settings)
    qp_id, identity_tag_id, stale_label, stale_tag_id = prepare_reconcile(
        radarr, name, opts=opts, ctx=ctx, service="Radarr", dry_run=dry_run
    )
    plan = plan_radarr(
        name=name,
        desired_movies=desired_movies,
        radarr_index=ctx.index,
        opts=opts,
        identity_tag_id=identity_tag_id,
        stale_tag_id=stale_tag_id,
        quality_profile_id=qp_id,
    )
    stale_tag_id = finalize_reconcile(
        radarr,
        name,
        plan,
        opts=opts,
        stale_label=stale_label,
        stale_tag_id=stale_tag_id,
        service="Radarr",
        dry_run=dry_run,
    )
    apply_radarr(
        radarr,
        plan,
        opts=opts,
        identity_tag_id=identity_tag_id,
        stale_tag_id=stale_tag_id,
        quality_profile_id=qp_id,
        dry_run=dry_run,
    )
