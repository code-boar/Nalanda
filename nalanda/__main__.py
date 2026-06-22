"""CLI entry point and per-collection orchestration.

Subcommands:
    run [<job>] [names...]
                        run every job kind (collections then metadata). `run
                        collections [names...]` runs only collections (optionally
                        scoped to named collections); `run metadata` runs only the
                        per-item metadata job.
    build <name> <src>  one-off: build a single collection from a source
    serve               run the webhook daemon
    schema [path]       regenerate config.schema.json from the config models

With no subcommand the argument is a bare source -- a TMDB collection id (digits), an
MDBList list URL / ``user/listname``, or ``tvdb:<id|slug|name>`` -- and Nalanda prints
a read-only report of how it resolves and its Jellyfin / Radarr / Sonarr status.
``--dry-run`` previews `run`/`serve` writes without mutating anything.

Usage:
    uv run python -m nalanda run
    uv run python -m nalanda 1234
    uv run python -m nalanda https://mdblist.com/lists/someuser/some-list
"""

from __future__ import annotations

import os
import sys
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from .artwork import ensure_folders, prune_folders, resolve_artwork
from .builders import run_builders, tmdb_image_url
from .cache import Cache, ttl_map
from .clients.jellyfin import JellyfinClient
from .clients.mdblist import MDBListClient
from .clients.radarr import RadarrClient, index_movies_by_tmdb
from .clients.sonarr import SonarrClient, index_series_by_ids
from .clients.tmdb import TMDBClient
from .clients.tvdb import TVDBClient
from .collection import build_jellyfin_collection
from .config import (
    CollectionDef,
    RadarrDefaults,
    Secrets,
    SonarrDefaults,
    json_schema,
    load_config,
)
from .http import HTTPError
from .logging import get_logger, setup_logging
from .matching import LibraryIndex, MediaRoutedIndex
from .metadata import plan_item_metadata, plan_metadata_cleanup
from .models import ImageSource, MediaItem, MediaType
from .radarr_sync import (
    RadarrContext,
    prime_radarr_context,
    reconcile_radarr,
    sweep_identity_tag,
)
from .sonarr_sync import (
    SonarrContext,
    prime_sonarr_context,
    reconcile_sonarr,
)
from .sonarr_sync import (
    sweep_identity_tag as sonarr_sweep_identity_tag,
)
from .sorting import build_forced_sort_name, jellyfin_sortable, section_prefix
from .state import load_state, run_lock, save_state
from .tagging import slugify

log = get_logger(__name__)


@dataclass
class Source:
    movies: list[MediaItem]
    label: str
    overview: str | None = None
    images: dict[str, str | None] = field(default_factory=dict)
    tmdb_collection_id: int | None = None


def _resolve_source(secrets: Secrets, arg: str) -> Source:
    """Resolve the CLI arg to a source: a TMDB collection id, or an MDBList list."""
    if arg.isdigit():
        if not secrets.tmdb_api_key:
            raise ValueError("TMDB_API_KEY is required to resolve a TMDB collection id")
        with TMDBClient(secrets.tmdb_api_key) as tmdb:
            collection = tmdb.get_collection(int(arg))
        return Source(
            movies=collection.movies,
            label=f"TMDB collection {collection.tmdb_id}: {collection.name}",
            overview=collection.overview,
            images={
                "Primary": tmdb_image_url(collection.poster_path),
                "Thumb": tmdb_image_url(collection.thumb_path),
                "Backdrop": tmdb_image_url(collection.backdrop_path),
            },
            tmdb_collection_id=collection.tmdb_id,
        )

    if not secrets.mdblist_api_key:
        raise ValueError(
            "MDBLIST_API_KEY is required to read an MDBList list; add it to .env"
        )
    with MDBListClient(secrets.mdblist_api_key) as mdb:
        return Source(movies=mdb.get_list(arg), label=f"MDBList list: {arg}")


def _match_jellyfin(secrets: Secrets, movies: list[MediaItem]) -> None:
    """READ-ONLY: report which movies exist in Jellyfin."""
    with JellyfinClient(secrets.jellyfin_url, secrets.jellyfin_api_key) as jf:
        info = jf.get_system_info()
        libraries = jf.resolve_libraries(collection_type="movies")
        log.info(
            "Jellyfin '%s' (v%s) - movie libraries: %s",
            info.get("ServerName"),
            info.get("Version"),
            ", ".join(lib.name for lib in libraries) or "(none)",
        )
        index = LibraryIndex(jf.get_movies([lib.id for lib in libraries]))
        log.info("Indexed %d movie(s) from Jellyfin.", index.size)
        result = index.match(movies)

    log.info(
        "Jellyfin match: %d present, %d missing.",
        len(result.matched),
        len(result.missing),
    )
    for movie, item in sorted(result.matched, key=lambda pair: pair[0].year or 0):
        log.info(
            "   [HAVE]    %s  %-45s jellyfin:%s",
            movie.year or "????",
            movie.title,
            item.id,
        )
    for movie in sorted(result.missing, key=lambda m: m.year or 0):
        log.info(
            "   [MISSING] %s  %-45s tmdb:%s",
            movie.year or "????",
            movie.title,
            movie.tmdb_id,
        )


def _report_radarr(secrets: Secrets, movies: list[MediaItem]) -> None:
    """READ-ONLY: report Radarr's view -- tracked, quality profile, tags."""
    with RadarrClient(secrets.radarr_url, secrets.radarr_api_key) as radarr:
        status = radarr.get_system_status()
        profiles = {p.id: p.name for p in radarr.get_quality_profiles()}
        tags = {t.id: t.label for t in radarr.get_tags()}
        index = index_movies_by_tmdb(radarr.get_movies())

    tracked = [(m, index[m.tmdb_id]) for m in movies if m.tmdb_id in index]
    log.info(
        "Radarr '%s' (v%s) - %d of %d movies tracked.",
        status.get("appName"),
        status.get("version"),
        len(tracked),
        len(movies),
    )
    for movie, rm in sorted(tracked, key=lambda pair: pair[0].year or 0):
        labels = [tags.get(t, f"#{t}") for t in rm.tags]
        qp = rm.quality_profile_id
        log.info(
            "   %s  %-40s profile:%-16s tags:%s",
            movie.year or "????",
            movie.title[:40],
            profiles.get(qp, "?") if qp is not None else "?",
            ", ".join(labels) or "-",
        )


def _report_sonarr(secrets: Secrets, shows: list[MediaItem]) -> None:
    """READ-ONLY: report Sonarr's view -- tracked, quality profile, tags."""
    from .sonarr_sync import match_series

    with SonarrClient(secrets.sonarr_url, secrets.sonarr_api_key) as sonarr:
        status = sonarr.get_system_status()
        profiles = {p.id: p.name for p in sonarr.get_quality_profiles()}
        tags = {t.id: t.label for t in sonarr.get_tags()}
        index = index_series_by_ids(sonarr.get_series())

    tracked = [(s, hit) for s in shows if (hit := match_series(s, index)) is not None]
    log.info(
        "Sonarr '%s' (v%s) - %d of %d shows tracked.",
        status.get("appName"),
        status.get("version"),
        len(tracked),
        len(shows),
    )
    for show, ss in sorted(tracked, key=lambda pair: pair[0].title):
        labels = [tags.get(t, f"#{t}") for t in ss.tags]
        qp = ss.quality_profile_id
        log.info(
            "   %-40s profile:%-16s tags:%s",
            show.title[:40],
            profiles.get(qp, "?") if qp is not None else "?",
            ", ".join(labels) or "-",
        )


def _build(secrets: Secrets, name: str, source_arg: str, sync_mode: str) -> int:
    """Resolve a source, then create/update the named Jellyfin collection, in order."""
    if not secrets.jellyfin_configured:
        log.error("Jellyfin is not configured; cannot build a collection.")
        return 1
    try:
        source = _resolve_source(secrets, source_arg)
    except Exception as exc:
        log.error("Could not resolve source %r: %s", source_arg, exc)
        return 1
    log.info("Source: %s - %d movie(s).", source.label, len(source.movies))

    lock_path = f"{secrets.nalanda_state}.lock"
    with (
        run_lock(path=lock_path),
        JellyfinClient(secrets.jellyfin_url, secrets.jellyfin_api_key) as jf,
    ):
        libraries = jf.resolve_libraries(collection_type="movies")
        index = LibraryIndex(jf.get_movies([lib.id for lib in libraries]))
        result = build_jellyfin_collection(
            jf,
            name,
            source.movies,
            index,
            sync_mode=sync_mode,
            tmdb_collection_id=source.tmdb_collection_id,
            overview=source.overview,
            desired_images={
                slot: ImageSource.from_url(url)
                for slot, url in source.images.items()
                if url
            },
        )
        log.info(
            "Collection %r: %s  (matched %d, missing %d)",
            name,
            result.plan.describe(),
            result.matched,
            result.missing,
        )
        if result.collection_id:
            items = jf.get_collection_items(result.collection_id)
            order = " -> ".join(f"{it.year}" for it in items)
            log.info("   id=%s  resulting order: %s", result.collection_id, order)
            for item in items:
                log.info("     %s  %s", item.year or "????", item.name)
    return 0


def _run(
    secrets: Secrets,
    names: list[str] | None = None,
    *,
    dry_run: bool = False,
    refresh_cache: bool = False,
    prune: bool | None = None,
) -> int:
    """Build every collection defined in config.yml (or just ``names``).

    With ``dry_run`` nothing is written -- Jellyfin and Radarr changes are only logged.

    ``prune`` controls the global maintenance pass (deleting collections + empty art
    folders no longer in config). It defaults to ``names is None`` -- a manual full
    ``run`` prunes, a scoped ``run <names>`` doesn't -- but the daemon decouples it:
    a *scoped* scheduled run on the default cron still prunes, so per-collection
    schedules don't lose orphan cleanup.
    """
    do_prune = (names is None) if prune is None else prune
    if not Path(secrets.nalanda_config).exists():
        log.error(
            "No config file at %s. A first `run`/`serve` seeds a starter there; "
            "edit it (and .env) and re-run.",
            secrets.nalanda_config,
        )
        return 1
    if not secrets.jellyfin_configured:
        log.error("Jellyfin is not configured; cannot build collections.")
        return 1

    cfg = load_config(secrets.nalanda_config)
    cache = (
        Cache(
            secrets.nalanda_cache,
            ttls=ttl_map(cfg.settings.cache),
            refresh=refresh_cache,
        )
        if cfg.settings.cache.enabled
        else None
    )
    tmdb = (
        TMDBClient(
            secrets.tmdb_api_key,
            language=cfg.settings.language,
            region=cfg.settings.region,
            cache=cache,
        )
        if secrets.tmdb_api_key
        else None
    )
    mdblist = (
        MDBListClient(secrets.mdblist_api_key, cache=cache)
        if secrets.mdblist_api_key
        else None
    )
    # Always available (Nalanda ships the TVDB key); login is lazy, so TVDB is never
    # contacted unless a tvdb_* builder actually runs.
    tvdb = TVDBClient(cache=cache)
    radarr = (
        RadarrClient(secrets.radarr_url, secrets.radarr_api_key, cache=cache)
        if secrets.radarr_configured
        else None
    )
    sonarr = (
        SonarrClient(secrets.sonarr_url, secrets.sonarr_api_key, cache=cache)
        if secrets.sonarr_configured
        else None
    )
    # State-file path derives from the config dir (see Secrets.nalanda_state).
    state_path = secrets.nalanda_state
    # Serialize real runs across processes (the daemon + any manual `run`). The lock
    # lives next to the state file. Dry-runs write nothing, so they skip it and can
    # preview anytime.
    lock_ctx = nullcontext() if dry_run else run_lock(path=f"{state_path}.lock")
    try:
        with (
            lock_ctx,
            JellyfinClient(secrets.jellyfin_url, secrets.jellyfin_api_key) as jf,
        ):
            movie_libraries = jf.resolve_libraries(collection_type="movies")
            movie_lib_by_name = {lib.name: lib.id for lib in movie_libraries}
            movie_index = LibraryIndex(
                jf.get_movies([lib.id for lib in movie_libraries])
            )
            log.info(
                "Indexed %d library movie(s). Building collections...", movie_index.size
            )

            # Show libraries + index are built lazily, only when a `media: tv`
            # collection is in scope (most runs are movies-only and shouldn't pay for a
            # show scan).
            show_lib_by_name: dict[str, str] = {}
            show_index_box: list[LibraryIndex] = []

            def show_index() -> LibraryIndex:
                if not show_index_box:
                    show_libraries = jf.resolve_libraries(collection_type="tvshows")
                    show_lib_by_name.update(
                        {lib.name: lib.id for lib in show_libraries}
                    )
                    idx = LibraryIndex(
                        jf.get_series([lib.id for lib in show_libraries])
                    )
                    log.info("Indexed %d library show(s).", idx.size)
                    show_index_box.append(idx)
                return show_index_box[0]

            # A collection's `libraries:` restricts matching to those libraries;
            # otherwise the whole movie/show library. Per (media, library-set) indexes
            # are built lazily + cached.
            lib_index_cache: dict[tuple[str, frozenset[str]], LibraryIndex] = {}

            def _media_index(media: str, libraries: list[str] | None) -> LibraryIndex:
                """The (cached) index for one media type, optionally filtered to named
                libraries.

                When ``libraries`` names only libraries of the *other* media (so none
                apply here), this media contributes nothing -> an empty index.
                """
                is_tv = media == "tv"
                base = show_index() if is_tv else movie_index
                lib_by_name = show_lib_by_name if is_tv else movie_lib_by_name
                getter = jf.get_series if is_tv else jf.get_movies
                if not libraries:
                    return base
                names = [name for name in libraries if name in lib_by_name]
                key = (media, frozenset(names))
                if key not in lib_index_cache:
                    ids = [lib_by_name[name] for name in names]
                    lib_index_cache[key] = LibraryIndex(getter(ids) if ids else [])
                return lib_index_cache[key]

            def index_for(coll: CollectionDef) -> LibraryIndex | MediaRoutedIndex:
                if coll.media == "mixed":
                    show_index()  # ensure show_lib_by_name is populated for validation
                    if coll.libraries:
                        known = set(movie_lib_by_name) | set(show_lib_by_name)
                        unknown = [n for n in coll.libraries if n not in known]
                        if unknown:
                            raise ValueError(
                                f"unknown libraries {unknown}; have {sorted(known)}"
                            )
                    return MediaRoutedIndex(
                        {m: _media_index(m, coll.libraries) for m in ("movie", "tv")}
                    )
                if coll.media == "tv":
                    show_index()  # populate show_lib_by_name before validating
                lib_by_name = (
                    show_lib_by_name if coll.media == "tv" else movie_lib_by_name
                )
                if coll.libraries:
                    unknown = [
                        name for name in coll.libraries if name not in lib_by_name
                    ]
                    if unknown:
                        raise ValueError(
                            f"unknown libraries {unknown}; have {list(lib_by_name)}"
                        )
                return _media_index(coll.media, coll.libraries)

            # The server's own sort lists, so our forced sort names match Jellyfin
            # exactly.
            remove_words, remove_chars, replace_chars = jf.get_sort_settings()

            def sortable(text: str) -> str:
                return jellyfin_sortable(
                    text,
                    remove_words=remove_words,
                    remove_chars=remove_chars,
                    replace_chars=replace_chars,
                )

            # Last-applied poster URLs, so we re-pull only when a URL actually changes.
            state = load_state(state_path)

            # Radarr context: fetched once iff an in-scope collection needs it.
            radarr_ctx: RadarrContext | None = None
            if radarr is not None and any(
                c.radarr is not None and c.radarr.enable
                for n, c in cfg.collections.items()
                if names is None or n in names
            ):
                radarr_ctx = prime_radarr_context(radarr)

            # Sonarr context: fetched once iff an in-scope collection needs it.
            sonarr_ctx: SonarrContext | None = None
            if sonarr is not None and any(
                c.sonarr is not None and c.sonarr.enable
                for n, c in cfg.collections.items()
                if names is None or n in names
            ):
                sonarr_ctx = prime_sonarr_context(sonarr)

            # Artwork repo upkeep: create a drop-folder per configured collection (over
            # the whole config, not just this run's subset) before we start resolving
            # images.
            ensure_folders(cfg.artwork_repo, cfg.collections.keys(), dry_run=dry_run)

            for cname, coll in cfg.collections.items():
                if names is not None and cname not in names:
                    continue
                try:
                    built = run_builders(
                        cname, coll, tmdb=tmdb, mdblist=mdblist, tvdb=tvdb
                    )
                    coll_index = index_for(coll)
                except Exception as exc:
                    log.error("  %-32s builder error: %s", cname, exc)
                    continue
                if not built.movies:
                    log.warning("  %-32s no movies from builders; skipping", cname)
                    continue

                config_overview = coll.overview
                tmdb_id = built.tmdb_collection_id
                # Desired metadata: config override wins, else the source (TMDB) value.
                overview = config_overview or built.overview

                # Resolve each slot: explicit *_art_url/*_art_file > artwork repo file
                # > auto-sourced TMDB image. Returns ImageSources carrying the apply
                # method (download vs. upload) and the idempotency marker.
                desired_images = resolve_artwork(
                    cname,
                    coll,
                    cfg.artwork_repo,
                    tmdb_images={
                        "Primary": built.primary_url,
                        "Thumb": built.thumb_url,
                        "Backdrop": built.backdrop_url,
                    },
                )
                # Section prefix (by position in cfg.sections) + the four-case sort
                # rule.
                prefix = (
                    section_prefix(cfg.sections.index(coll.section), len(cfg.sections))
                    if coll.section
                    else None
                )
                forced_sort_name = build_forced_sort_name(
                    cname, prefix=prefix, sort_title=coll.sort_title, sortable=sortable
                )
                # Unset order defaults to the build's natural order: release_date for a
                # release-sorted pool, source for a sole curated list (server order).
                order = coll.order or (
                    "release_date" if built.release_sorted else "source"
                )
                hide_year = (
                    coll.hide_year
                    if coll.hide_year is not None
                    else cfg.settings.hide_year
                )
                result = build_jellyfin_collection(
                    jf,
                    cname,
                    built.movies,
                    coll_index,
                    sync_mode=coll.sync_mode or cfg.settings.sync_mode,
                    order=order,
                    tmdb_collection_id=tmdb_id,
                    overview=overview,
                    desired_images=desired_images,
                    current_image_markers=state.get(cname, {}),
                    forced_sort_name=forced_sort_name,
                    hide_year=hide_year,
                    apply=not dry_run,
                )
                # Remember each slot's marker, so the next run only re-applies on a
                # change.
                if result.collection_id:
                    state[cname] = {
                        slot: (src.marker if src else None)
                        for slot, src in desired_images.items()
                    }
                log.info(
                    "  %-32s %-26s (matched %d, missing %d)",
                    cname,
                    result.plan.describe(),
                    result.matched,
                    result.missing,
                )

                # Radarr drives the movie members, Sonarr the show members. In a `mixed`
                # collection both run, each on its own media subset; for a movie/tv
                # collection one subset is the whole set and the other is empty.
                radarr_movies = [m for m in built.movies if m.media_type == "movie"]
                sonarr_shows = [m for m in built.movies if m.media_type == "tv"]

                # --- Radarr reconciliation (declarative identity-tag sync) ---
                if coll.radarr is not None and not coll.radarr.enable:
                    log.debug(
                        "  %-32s radarr: enable=false; block present but not managed",
                        cname,
                    )
                if (
                    radarr is not None
                    and coll.radarr is not None
                    and coll.radarr.enable
                    and radarr_ctx is not None
                ):
                    try:
                        reconcile_radarr(
                            radarr,
                            coll,
                            cname,
                            settings=cfg.settings,
                            ctx=radarr_ctx,
                            desired_movies=radarr_movies,
                            dry_run=dry_run,
                        )
                    except Exception as exc:
                        log.error("  %-32s radarr error: %s", cname, exc)

                # --- Sonarr reconciliation (declarative identity-tag sync) ---
                if coll.sonarr is not None and not coll.sonarr.enable:
                    log.debug(
                        "  %-32s sonarr: enable=false; block present but not managed",
                        cname,
                    )
                if (
                    sonarr is not None
                    and coll.sonarr is not None
                    and coll.sonarr.enable
                    and sonarr_ctx is not None
                ):
                    try:
                        reconcile_sonarr(
                            sonarr,
                            coll,
                            cname,
                            settings=cfg.settings,
                            ctx=sonarr_ctx,
                            desired_shows=sonarr_shows,
                            dry_run=dry_run,
                        )
                    except Exception as exc:
                        log.error("  %-32s sonarr error: %s", cname, exc)

            # Prune empty artwork folders for collections no longer configured. Pruning
            # runs (a subset `run <names>` must not prune folders for the collections it
            # skipped); the daemon sets prune=True on the default-cron run so scheduled
            # scopes still clean up.
            if do_prune:
                prune_folders(cfg.artwork_repo, cfg.collections.keys(), dry_run=dry_run)

            # Prune collections Nalanda previously made that are gone from the config.
            # Keyed on the state file -> only ever our own collections, never hand-made
            # BoxSets. Pruning runs only (a subset `run <names>` must not delete the
            # unlisted ones).
            if cfg.settings.delete_unconfigured_collections and do_prune:
                rdefaults = cfg.settings.radarr or RadarrDefaults()
                for orphan in [n for n in list(state) if n not in cfg.collections]:
                    existing = jf.find_collection(orphan)
                    if existing is not None:
                        if dry_run:
                            log.info(
                                "  %-32s [dry-run] would delete (no longer in config)",
                                orphan,
                            )
                        else:
                            jf.delete_item(existing.id)
                            log.info("  %-32s deleted (no longer in config)", orphan)
                    # The whole collection is gone -> all members left. Sweep its
                    # identity tag per the global stale policy. NOTE: best-effort -- a
                    # deleted collection that used a custom per-collection `tag:`
                    # override can't be reconstructed (its config is gone), so the
                    # global tag_prefix + name slug is assumed.
                    if radarr is not None:
                        identity_label = f"{rdefaults.tag_prefix}{slugify(orphan)}"
                        try:
                            sweep_identity_tag(
                                radarr,
                                identity_label=identity_label,
                                stale_label=f"{identity_label}{rdefaults.stale_suffix}",
                                policy=rdefaults.stale_tags,
                                dry_run=dry_run,
                            )
                        except Exception as exc:
                            log.error("  %-32s radarr tag sweep error: %s", orphan, exc)
                    # The orphan's media is unknown (its config is gone), so sweep
                    # Sonarr too -- the sweep is a no-op unless that identity tag
                    # actually exists in Sonarr.
                    if sonarr is not None:
                        sdefaults = cfg.settings.sonarr or SonarrDefaults()
                        s_label = f"{sdefaults.tag_prefix}{slugify(orphan)}"
                        try:
                            sonarr_sweep_identity_tag(
                                sonarr,
                                identity_label=s_label,
                                stale_label=f"{s_label}{sdefaults.stale_suffix}",
                                policy=sdefaults.stale_tags,
                                dry_run=dry_run,
                            )
                        except Exception as exc:
                            log.error("  %-32s sonarr tag sweep error: %s", orphan, exc)
                    if not dry_run:
                        state.pop(orphan, None)

            if not dry_run:
                save_state(state_path, state)
            if cache is not None:
                log.info("cache: %s", cache.summary)
    finally:
        if tmdb:
            tmdb.close()
        if mdblist:
            mdblist.close()
        if tvdb:
            tvdb.close()
        if radarr:
            radarr.close()
        if sonarr:
            sonarr.close()
    return 0


def _run_metadata(secrets: Secrets, *, dry_run: bool = False) -> int:
    """Write + lock per-item metadata overrides from ``config.metadata`` into Jellyfin.

    Idempotent: each declared item is matched by provider id, its live DTO compared
    against the desired fields, and only differences written (and the field locked).
    With ``settings.unlock_unconfigured_metadata`` set, fields removed from config are
    unlocked.
    """
    if not Path(secrets.nalanda_config).exists():
        log.error("No config file at %s.", secrets.nalanda_config)
        return 1
    if not secrets.jellyfin_configured:
        log.error("Jellyfin is not configured; cannot write metadata.")
        return 1

    cfg = load_config(secrets.nalanda_config)
    md = cfg.metadata
    movies = md.movies if md else []
    shows = md.shows if md else []
    state_path = secrets.nalanda_metadata_state
    cleanup = cfg.settings.unlock_unconfigured_metadata

    # Nothing to do unless there are entries, or cleanup might act on existing state.
    if not movies and not shows and not (cleanup and Path(state_path).exists()):
        log.info("No metadata entries configured; nothing to do.")
        return 0

    lock_ctx = nullcontext() if dry_run else run_lock(path=f"{state_path}.lock")
    with (
        lock_ctx,
        JellyfinClient(secrets.jellyfin_url, secrets.jellyfin_api_key) as jf,
    ):
        movie_index = (
            LibraryIndex(
                jf.get_movies(
                    [lib.id for lib in jf.resolve_libraries(collection_type="movies")]
                )
            )
            if movies
            else LibraryIndex([])
        )
        show_index = (
            LibraryIndex(
                jf.get_series(
                    [lib.id for lib in jf.resolve_libraries(collection_type="tvshows")]
                )
            )
            if shows
            else LibraryIndex([])
        )
        index = MediaRoutedIndex({"movie": movie_index, "tv": show_index})
        prefix = "[dry-run] " if dry_run else ""

        state = load_state(state_path)
        new_state: dict[str, Any] = dict(state)
        declared: dict[str, set[str]] = {}
        matched: set[str] = set()  # declared keys actually found + written this run

        for media, entries in (("movie", movies), ("tv", shows)):
            for entry in entries:
                key = entry.provider_key
                desired = entry.field_values()
                declared[key] = set(desired)
                try:
                    query = MediaItem(
                        media_type=cast(MediaType, media),
                        tmdb_id=entry.tmdb,
                        imdb_id=entry.imdb,
                        tvdb_id=entry.tvdb,
                        title=key,
                    )
                    hit = index.find(query)
                    if hit is None:
                        log.warning("  metadata %-20s not in library; skipping", key)
                        continue
                    plan = plan_item_metadata(desired, jf.get_item(hit.id))
                    if not plan.up_to_date and not dry_run:
                        jf.update_item(hit.id, plan.changes)
                    log.info("  metadata %-20s %s%s", key, prefix, plan.describe())
                    new_state[key] = {"jellyfin_id": hit.id, "fields": sorted(desired)}
                    matched.add(key)
                except Exception as exc:
                    log.error("  metadata %-20s error: %s", key, exc)

        # Cleanup: unlock fields previously managed but no longer declared.
        if cleanup:
            for key, rec in list(state.items()):
                orphan = sorted(set(rec.get("fields") or []) - declared.get(key, set()))
                if not orphan:
                    continue
                jid = rec.get("jellyfin_id")
                try:
                    try:
                        dto = jf.get_item(jid) if jid else None
                    except HTTPError:
                        dto = None
                    if dto is None:
                        new_state.pop(key, None)
                        continue
                    changes = plan_metadata_cleanup(orphan, dto)
                    if changes:
                        if not dry_run:
                            jf.update_item(jid, changes)
                        log.info(
                            "  metadata cleanup %-20s %sunlock %s", key, prefix, orphan
                        )
                    else:
                        log.debug(
                            "  metadata cleanup %-20s already unlocked %s", key, orphan
                        )
                    if key not in declared or key not in matched:
                        # Drop the record for keys no longer declared (true orphans)
                        # and for declared keys we couldn't match/write this run --
                        # the main loop is the sole authority on a matched key's
                        # record, so don't resurrect an unwritten one here with a
                        # stale id/fields.
                        new_state.pop(key, None)
                except Exception as exc:
                    log.error("  metadata cleanup %-20s error: %s", key, exc)

        if not dry_run:
            save_state(state_path, new_state)
    return 0


# Job kinds a `run` can dispatch, in the order a global run executes them.
RUN_JOBS = ("collections", "metadata")


def _dispatch_run(
    secrets: Secrets,
    targets: list[str],
    *,
    dry_run: bool = False,
    refresh_cache: bool = False,
) -> int:
    """Dispatch ``run [<job>] [names...]`` to one or all job kinds.

    No job word runs everything (collections then metadata); a ``collections`` or
    ``metadata`` job word runs just that one. Collection names scope the collections
    job only -- metadata is all-or-nothing. Jobs run sequentially and are isolated:
    one job's failure is logged but does not stop the others, and the exit code is
    nonzero if any job failed.
    """
    if not targets:
        jobs: list[str] = list(RUN_JOBS)
        names: list[str] | None = None
    elif targets[0] == "collections":
        jobs = ["collections"]
        names = targets[1:] or None
    elif targets[0] == "metadata":
        if len(targets) > 1:
            log.error(
                "`run metadata` takes no collection names (got %s); metadata is "
                "all-or-nothing.",
                targets[1:],
            )
            return 1
        jobs = ["metadata"]
        names = None
    else:
        log.error(
            "Unknown run target %r. Use `run` (everything), "
            "`run collections [names...]`, or `run metadata`. To scope to a "
            "collection named %r, write `run collections %r`.",
            targets[0],
            targets[0],
            targets[0],
        )
        return 1

    # Dispatch table keyed by job kind. A kind added to RUN_JOBS without a runner
    # here raises KeyError (caught below) rather than silently running as another
    # kind.
    runners = {
        "collections": lambda: _run(
            secrets, names=names, dry_run=dry_run, refresh_cache=refresh_cache
        ),
        "metadata": lambda: _run_metadata(secrets, dry_run=dry_run),
    }
    exit_code = 0
    for job in jobs:
        try:
            rc = runners[job]()
        except Exception as exc:  # one job's failure must not stop the others
            log.error("%s job failed: %s", job, exc)
            rc = 1
        if rc:
            exit_code = 1
    return exit_code


def _write_schema(path: str) -> int:
    """Generate the JSON Schema for config.yml and write it to ``path``."""
    import json

    out = Path(path)
    out.write_text(json.dumps(json_schema(), indent=2) + "\n", encoding="utf-8")
    log.info("Wrote JSON schema to %s", out)
    return 0


def _cache_command(secrets: Secrets, args: list[str]) -> int:
    """``nalanda cache info|prune|clear [namespace]`` -- inspect or maintain the cache
    file."""
    from .config import CacheSettings

    sub = args[0] if args else "info"
    # TTLs (needed by `prune`) come from the config if it loads, else the defaults.
    settings = CacheSettings()
    if Path(secrets.nalanda_config).exists():
        try:
            settings = load_config(secrets.nalanda_config).settings.cache
        except Exception:
            pass
    cache = Cache(secrets.nalanda_cache, ttls=ttl_map(settings))

    if sub == "info":
        counts = cache.namespaces()
        log.info("Cache: %s", secrets.nalanda_cache)
        for namespace in sorted(counts):
            log.info("  %-22s %d", namespace, counts[namespace])
        if not counts:
            log.info("  (empty)")
        return 0
    if sub == "prune":
        removed = cache.prune_expired()
        log.info(
            "Pruned %d expired cache entr%s.", removed, "y" if removed == 1 else "ies"
        )
        return 0
    if sub == "clear":
        if len(args) > 1:
            cache.purge(args[1])
            log.info("Cleared cache namespace %r.", args[1])
        else:
            Path(secrets.nalanda_cache).unlink(missing_ok=True)
            log.info("Cleared the entire cache (%s).", secrets.nalanda_cache)
        return 0
    log.error("Usage: nalanda cache [info | prune | clear [namespace]]")
    return 1


_USAGE = """\
Nalanda -- a simplified collection and metadata manager for Jellyfin,
inspired by Kometa.

Usage:
  nalanda run [collections [name...] | metadata]    build collections, then metadata
  nalanda build "<name>" <source> [sync|append]     one-off build of one collection
  nalanda serve                                     run the webhook daemon
  nalanda schema [path]                             regenerate the config JSON schema
  nalanda cache [info | prune | clear [namespace]]  inspect/maintain the metadata cache
  nalanda <tmdb-id | mdblist-url | tvdb:<ref>>      read-only report for a source

Options:
  --dry-run         log intended writes without performing them
  --refresh-cache   re-fetch and re-store the cache for this run (run only)
  --version, -V     print the version and exit
  --help, -h        show this help and exit"""


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in args
    if dry_run:
        args = [a for a in args if a != "--dry-run"]
    # --refresh-cache forces a cache miss for this run (re-fetch + re-store). It
    # applies to `run` only -- the report and `build` always read fresh; `serve`
    # strips and ignores it.
    refresh_cache = "--refresh-cache" in args
    if refresh_cache:
        args = [a for a in args if a != "--refresh-cache"]
    # --version / --help short-circuit before any config or connection work, and write
    # to stdout (not the logger) so the output stays clean and parseable.
    if args and args[0] in ("--version", "-V"):
        from . import __version__

        print(__version__)
        return 0
    if args and args[0] in ("--help", "-h"):
        print(_USAGE)
        return 0
    # Read the config path from the environment so `.env` can live next to config.yml
    # (both default to the working dir; the Docker image points NALANDA_CONFIG at
    # /config). Real environment variables still override any value found in that `.env`
    # file.
    config_path = os.environ.get("NALANDA_CONFIG", "config.yml")
    env_path = Path(config_path).parent / ".env"
    secrets = Secrets(_env_file=str(env_path))  # pyright: ignore[reportCallIssue]

    # `schema` runs before anything else -- it must work even if the config is invalid.
    if args and args[0] == "schema":
        return _write_schema(args[1] if len(args) > 1 else "config.schema.json")

    # `cache` is a maintenance command on the cache file -- independent of config
    # validity.
    if args and args[0] == "cache":
        return _cache_command(secrets, args[1:])

    # A first `run`/`serve` with no config seeds a starter config.yml + .env next to it,
    # so a fresh deployment comes up with files to edit instead of erroring on a missing
    # config. The editor schema is (re)written beside it every time -- a generated
    # artifact pinned to this version, so an upgrade refreshes it on the next restart.
    if args and args[0] in ("run", "serve"):
        from .bootstrap import ensure_config_scaffold, refresh_config_schema

        ensure_config_scaffold(secrets.nalanda_config)
        refresh_config_schema(secrets.nalanda_config)

    if Path(secrets.nalanda_config).exists():
        cfg = load_config(secrets.nalanda_config)
        log.info(
            "Loaded %s: %d collection(s) defined.",
            secrets.nalanda_config,
            len(cfg.collections),
        )

    if args and args[0] == "run":
        return _dispatch_run(
            secrets, args[1:], dry_run=dry_run, refresh_cache=refresh_cache
        )

    if args and args[0] == "serve":
        from .server import serve

        return serve(secrets, dry_run=dry_run)

    if args and args[0] == "build":
        if len(args) < 3:
            log.error('Usage: python -m nalanda build "<name>" <source> [sync|append]')
            return 1
        return _build(secrets, args[1], args[2], args[3] if len(args) > 3 else "sync")

    log.info(
        "Connections -> Jellyfin: %s | Radarr: %s | Sonarr: %s | MDBList key: %s",
        "yes" if secrets.jellyfin_configured else "no",
        "yes" if secrets.radarr_configured else "no",
        "yes" if secrets.sonarr_configured else "no",
        "yes" if secrets.mdblist_configured else "no",
    )

    if not args:
        log.info(
            "No subcommand given. Use `run`, `serve`, `build`, or `schema`, or pass a"
            " source to inspect (a TMDB collection id, an MDBList list URL, or"
            " `tvdb:<ref>`)."
        )
        return 0

    arg = args[0]

    # `tvdb:NNN` (or a slug/name) -> a single show: report its Sonarr status, read-only.
    # TVDB needs no key from the user -- Nalanda ships one.
    if arg.startswith("tvdb:"):
        ref = arg.split(":", 1)[1]
        with TVDBClient() as tvdb:
            show = tvdb.resolve(ref, media="tv")
        log.info(
            "TVDB show: %s (tvdb:%s tmdb:%s)", show.title, show.tvdb_id, show.tmdb_id
        )
        if secrets.sonarr_configured:
            _report_sonarr(secrets, [show])
        else:
            log.info("Sonarr not configured; resolved show only.")
        return 0

    try:
        source = _resolve_source(secrets, arg)
    except Exception as exc:
        log.error("Could not resolve source %r: %s", arg, exc)
        return 1

    log.info("%s - %d movie(s).", source.label, len(source.movies))

    if secrets.jellyfin_configured:
        _match_jellyfin(secrets, source.movies)
    else:
        log.info("Jellyfin not configured; listing source only.")
        for movie in sorted(source.movies, key=lambda m: m.year or 0):
            log.info(
                "   %s  %s  tmdb:%s imdb:%s",
                movie.year or "????",
                movie.title,
                movie.tmdb_id,
                movie.imdb_id,
            )

    if secrets.radarr_configured:
        _report_radarr(secrets, source.movies)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
