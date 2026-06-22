"""Configuration loading.

Two sources, deliberately split:

* **Secrets** (API keys + base URLs) come from the environment / ``.env`` and are
  never committed.
* **The collection config** (collections, builders, per-collection settings) comes
  from a human-edited ``config.yml``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .cache import parse_duration
from .tagging import slugify


def _is_schedule_off(value: str) -> bool:
    """True if a schedule reference explicitly disables scheduling
    (``none`` / ``disabled``)."""
    return value.strip().lower() in ("none", "disabled")


class Secrets(BaseSettings):
    """Per-deployment launch config from the environment / ``.env``: secrets, the
    service URLs Nalanda connects to, and the deployment knobs (bind host/port,
    config path).

    The state, cache, and metadata-state file paths are not separate knobs -- they
    derive from the config file's directory (see :attr:`nalanda_state` /
    :attr:`nalanda_cache` / :attr:`nalanda_metadata_state`), so pointing
    ``NALANDA_CONFIG`` at a mounted volume puts all three there too.

    This is the "where/how this install runs" channel, kept separate from
    ``config.yml`` (the "what to build + how Nalanda behaves" channel). Real
    environment variables win over ``.env`` values, so a container's ``ENV``
    overrides a mounted file.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    tmdb_api_key: str = ""
    jellyfin_url: str = ""
    jellyfin_api_key: str = ""
    radarr_url: str = ""
    radarr_api_key: str = ""
    sonarr_url: str = ""
    sonarr_api_key: str = ""
    mdblist_api_key: str = ""
    webhook_secret: str = (
        ""  # shared secret for the `serve` webhook daemon (env WEBHOOK_SECRET)
    )

    # Deployment knobs (not secrets, but per-install launch config). The default host is
    # loopback so a bare `serve` isn't network-exposed; set NALANDA_HOST=0.0.0.0 to
    # publish it (the Docker image does) -- behind a TLS reverse proxy, as the token is
    # sent in clear.
    nalanda_host: str = "127.0.0.1"  # webhook bind address (env NALANDA_HOST)
    nalanda_port: int = 8842  # webhook listen port (env NALANDA_PORT)
    nalanda_config: str = "config.yml"  # config-file path (env NALANDA_CONFIG)

    @property
    def jellyfin_configured(self) -> bool:
        return bool(self.jellyfin_url and self.jellyfin_api_key)

    @property
    def radarr_configured(self) -> bool:
        return bool(self.radarr_url and self.radarr_api_key)

    @property
    def sonarr_configured(self) -> bool:
        return bool(self.sonarr_url and self.sonarr_api_key)

    @property
    def mdblist_configured(self) -> bool:
        return bool(self.mdblist_api_key)

    @property
    def nalanda_state(self) -> str:
        """State-file path: a sibling of the config file
        (``<config-dir>/.nalanda-state.json``)."""
        return str(Path(self.nalanda_config).parent / ".nalanda-state.json")

    @property
    def nalanda_cache(self) -> str:
        """Cache-file path: a sibling of the config file
        (``<config-dir>/.nalanda-cache.db``)."""
        return str(Path(self.nalanda_config).parent / ".nalanda-cache.db")

    @property
    def nalanda_metadata_state(self) -> str:
        """Metadata state-file path: a sibling of the config file. Kept separate from
        ``nalanda_state`` so the collection orphan sweep never sees metadata keys."""
        return str(Path(self.nalanda_config).parent / ".nalanda-metadata-state.json")


class _ArrOptionsBase(BaseModel):
    """Per-collection override fields shared by Radarr and Sonarr
    (all optional -> inherit).

    Unset (``None``) fields fall back to the matching
    ``settings.{radarr,sonarr}`` default.
    """

    model_config = ConfigDict(extra="forbid")

    enable: bool = Field(
        default=False,
        description=(
            "Opt this collection into *arr management. Defaults to false, so a block "
            "with `enable` unset or false is inert (no add/tag/search) -- doubling as "
            "an off switch."
        ),
    )
    add_missing: bool | None = None
    add_existing: bool | None = None
    upgrade_existing: bool | None = None
    monitor_existing: bool | None = None
    search: bool | None = None
    quality_profile: str | int | None = None
    root_folder: str | None = None
    tag_prefix: str | None = None
    stale_tags: Literal["delete", "mark", "keep"] | None = None
    stale_suffix: str | None = None
    tag: str | None = Field(
        default=None,
        description=(
            "Override the identity tag's auto-slug (final label = tag_prefix + this). "
            "Defaults to a slug of the collection name."
        ),
    )


@dataclass(frozen=True)
class _ResolvedArr:
    """Resolved options common to both arrs (collection override -> global default)."""

    add_missing: bool
    add_existing: bool
    upgrade_existing: bool
    monitor_existing: bool
    search: bool
    quality_profile: str | int | None
    root_folder: str | None
    tag_prefix: str
    stale_tags: str
    stale_suffix: str
    tag: (
        str | None
    )  # identity-tag slug override (None -> derive from the collection name)


def _effective[R: _ResolvedArr](
    opt: _ArrOptionsBase,
    base: BaseModel,
    inherited: tuple[str, ...],
    cls: type[R],
) -> R:
    """Resolve override-or-default for each inherited field, then build ``cls``."""
    resolved = {
        f: (v if (v := getattr(opt, f)) is not None else getattr(base, f))
        for f in inherited
    }
    return cls(tag=opt.tag, **resolved)


class RadarrDefaults(BaseModel):
    """Global Radarr sync + tagging defaults.

    Per-collection ``radarr:`` blocks override any of these. All four ``add_*``/
    ``*_existing`` toggles default **false**: with everything off, a
    Radarr-managed collection is a no-op until you opt into adding and/or tagging.
    """

    model_config = ConfigDict(extra="forbid")

    add_missing: bool = Field(
        default=False, description="Add collection movies that aren't yet in Radarr."
    )
    add_existing: bool = Field(
        default=False,
        description="Apply the identity tag to collection movies already in Radarr "
        "(the 'if-exists' half of tagging; decoupled from adding).",
    )
    upgrade_existing: bool = Field(
        default=False,
        description=(
            "Reset existing members' quality profile to the collection's (diff-first)."
        ),
    )
    monitor_existing: bool = Field(
        default=False,
        description="Reset existing members' monitored flag to `monitored`.",
    )
    monitored: bool = Field(default=True, description="Monitor newly added movies.")
    search: bool = Field(
        default=False,
        description=(
            "Trigger a Radarr search when adding a movie "
            "(fully decoupled from tagging)."
        ),
    )
    minimum_availability: Literal["announced", "in_cinemas", "released"] = Field(
        default="released", description="Radarr minimum availability for added movies."
    )
    quality_profile: str | int | None = Field(
        default=None,
        description="Quality profile name or id for added (and upgraded) movies.",
    )
    root_folder: str | None = Field(
        default=None, description="Root folder path for added movies."
    )
    tag_prefix: str = Field(
        default="nalanda-",
        description=(
            "Namespace prefix for Nalanda identity tags. Nalanda only ever "
            "adds/removes tags under this prefix, never touching manual or "
            "other-tool tags."
        ),
    )
    stale_tags: Literal["delete", "mark", "keep"] = Field(
        default="mark",
        description=(
            "What happens to a movie's identity tag when it leaves a collection: "
            "delete it, mark it (swap to <tag><stale_suffix>), or keep it."
        ),
    )
    stale_suffix: str = Field(
        default="-stale",
        description="Suffix appended to the identity tag for the `mark` policy.",
    )


class RadarrOptions(_ArrOptionsBase):
    """Per-collection Radarr options (field meanings: see :class:`RadarrDefaults`)."""

    monitored: bool | None = None
    minimum_availability: Literal["announced", "in_cinemas", "released"] | None = None


# Derived from the model so it cannot silently drift: every Options override field
# except `enable`/`tag` is inherited from settings.radarr.
_RADARR_INHERITED = tuple(
    f for f in RadarrOptions.model_fields if f not in ("enable", "tag")
)


@dataclass(frozen=True)
class ResolvedRadarr(_ResolvedArr):
    """A Radarr-managed collection's options after global+collection resolution."""

    monitored: bool
    minimum_availability: str


def effective_radarr(coll: CollectionDef, settings: GlobalSettings) -> ResolvedRadarr:
    """Resolve a Radarr collection's options: collection override, else global
    default."""
    return _effective(
        coll.radarr or RadarrOptions(),
        settings.radarr or RadarrDefaults(),
        _RADARR_INHERITED,
        ResolvedRadarr,
    )


# Sonarr's episode/season monitoring strategy. The friendly names map to Sonarr's API
# spellings (firstSeason/latestSeason) in sonarr_sync. This is the one axis movies lack:
# which episodes/seasons a show monitors.
SonarrMonitor = Literal[
    "all",
    "future",
    "missing",
    "existing",
    "pilot",
    "first_season",
    "latest_season",
    "none",
]


class SonarrDefaults(BaseModel):
    """Global Sonarr sync + tagging defaults (the TV analogue of RadarrDefaults).

    Per-collection ``sonarr:`` blocks override any of these. The
    ``add_*``/``*_existing`` toggles default **false**, so even an enabled
    collection adds/tags nothing until you opt in.
    """

    model_config = ConfigDict(extra="forbid")

    add_missing: bool = Field(
        default=False, description="Add collection shows not yet in Sonarr."
    )
    add_existing: bool = Field(
        default=False, description="Apply the identity tag to shows already in Sonarr."
    )
    upgrade_existing: bool = Field(
        default=False,
        description="Reset existing members' quality profile to the collection's.",
    )
    monitor_existing: bool = Field(
        default=False,
        description=(
            "Reconcile existing members' `monitored` flag and season monitoring to "
            "`monitor` each run (season sync via Sonarr's seasonpass)."
        ),
    )
    monitored: bool = Field(
        default=True, description="Series-level monitored flag for added shows."
    )
    monitor: SonarrMonitor = Field(
        default="all",
        description="Episode/season monitoring strategy for added shows (and, with "
        "monitor_existing, reconciled): all | future | missing | existing | pilot | "
        "first_season | latest_season | none.",
    )
    search: bool = Field(
        default=False,
        description=(
            "Search for missing episodes when adding a show (decoupled from tagging)."
        ),
    )
    cutoff_search: bool = Field(
        default=False, description="Also search for cutoff-unmet episodes when adding."
    )
    series_type: Literal["standard", "daily", "anime"] = Field(
        default="standard", description="Sonarr series type (episode numbering scheme)."
    )
    season_folder: bool = Field(
        default=True, description="Organise episodes into season folders."
    )
    quality_profile: str | int | None = Field(
        default=None,
        description="Quality profile name or id for added (and upgraded) shows.",
    )
    language_profile: str | int | None = Field(
        default=None,
        description=(
            "Language profile name or id (Sonarr v3 only; ignored on v4 which"
            " dropped them)."
        ),
    )
    root_folder: str | None = Field(
        default=None, description="Root folder path for added shows."
    )
    tag_prefix: str = Field(
        default="nalanda-",
        description=(
            "Namespace prefix for Nalanda identity tags; Nalanda only ever "
            "touches this."
        ),
    )
    stale_tags: Literal["delete", "mark", "keep"] = Field(
        default="mark",
        description=(
            "What happens to a show's identity tag when it leaves a collection: "
            "delete | mark (swap to <tag><stale_suffix>) | keep."
        ),
    )
    stale_suffix: str = Field(
        default="-stale",
        description="Suffix appended to the identity tag for the `mark` policy.",
    )


class SonarrOptions(_ArrOptionsBase):
    """Per-collection Sonarr options (field meanings: see :class:`SonarrDefaults`)."""

    monitored: bool | None = None
    monitor: SonarrMonitor | None = None
    cutoff_search: bool | None = None
    series_type: Literal["standard", "daily", "anime"] | None = None
    season_folder: bool | None = None
    language_profile: str | int | None = None


# Derived from the model so it cannot silently drift: every Options override field
# except `enable`/`tag` is inherited from settings.sonarr.
_SONARR_INHERITED = tuple(
    f for f in SonarrOptions.model_fields if f not in ("enable", "tag")
)


@dataclass(frozen=True)
class ResolvedSonarr(_ResolvedArr):
    """A Sonarr-managed collection's options after global+collection resolution."""

    monitored: bool
    monitor: str
    cutoff_search: bool
    series_type: str
    season_folder: bool
    language_profile: str | int | None


def effective_sonarr(coll: CollectionDef, settings: GlobalSettings) -> ResolvedSonarr:
    """Resolve a Sonarr collection's options: collection override, else global
    default."""
    return _effective(
        coll.sonarr or SonarrOptions(),
        settings.sonarr or SonarrDefaults(),
        _SONARR_INHERITED,
        ResolvedSonarr,
    )


class WebhookSettings(BaseModel):
    """Behaviour of the ``nalanda serve`` webhook daemon (coalescing + gating of
    inbound requests).

    Time-driven scheduling lives in ``settings.run_schedules`` /
    ``settings.run_schedule`` / ``settings.jobs``, not here -- a cron is not a
    webhook concern. The bind address/port are *deployment* knobs, not behaviour,
    so they live in the environment / ``.env`` (``NALANDA_HOST`` /
    ``NALANDA_PORT``; see :class:`Secrets`), not here. The state/cache file paths
    derive from the config dir.
    """

    model_config = ConfigDict(extra="forbid")

    debounce_seconds: int = Field(
        default=300,
        description=(
            "Coalesce a burst of triggers into one run; the window resets on each "
            "new trigger and the run fires once it's been quiet this long."
        ),
    )
    max_wait_seconds: int | None = Field(
        default=None,
        description="Optional cap on total debounce wait, so a sustained burst can't "
        "postpone the run indefinitely.",
    )
    allow_full_run: bool = Field(
        default=False,
        description="Permit POST /run to trigger a full run. Off -> /run returns 403; "
        "/trigger is always scoped and never runs everything.",
    )


class CacheSettings(BaseModel):
    """Metadata-cache durations + on/off switch.

    Four intent-named knobs each govern a group of internal cache namespaces; the
    Radarr/Sonarr add-lookup caches are fixed internal constants, not exposed here.
    Durations are ``<int>d`` / ``<int>h`` (or ``0`` / ``off`` to bypass that
    bucket); minutes are rejected. The cache file path is not configured here --
    it derives from the config directory (see :class:`Secrets`).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=True, description="Master switch for the metadata cache."
    )
    record_cache_duration: str = Field(
        default="30d",
        description=(
            "Per-id details + name/id resolution (TMDB, TVDB). <int>d / <int>h, "
            "or 0/off to bypass."
        ),
    )
    list_cache_duration: str = Field(
        default="1d",
        description="Membership of curated/dynamic lists (MDBList, TVDB, TMDB lists).",
    )
    query_cache_duration: str = Field(
        default="3d",
        description="discover / genre / company / keyword / person queries.",
    )
    chart_cache_duration: str = Field(
        default="1d",
        description="popular / trending / time-windowed charts.",
    )

    @model_validator(mode="after")
    def _check_durations(self) -> CacheSettings:
        for field in (
            "record_cache_duration",
            "list_cache_duration",
            "query_cache_duration",
            "chart_cache_duration",
        ):
            parse_duration(getattr(self, field))  # raises on minutes / garbage
        return self


class JobSchedules(BaseModel):
    """Per-job-kind default run schedules (level 2 of the run-schedule cascade).

    Each value is a schedule *reference*: a key in ``settings.run_schedules``, an
    inline cron string, or the sentinel ``none`` / ``disabled`` (opt out). Two job
    kinds are wired: ``collections`` (the collection build/reconcile job) and
    ``metadata`` (the per-item metadata job). Unknown kinds are rejected so a
    typo'd key surfaces immediately.
    """

    model_config = ConfigDict(extra="forbid")

    collections: str | None = Field(
        default=None,
        description=(
            "Default run schedule for the collection build/reconcile job. A name in "
            "`settings.run_schedules`, an inline cron, or 'none'/'disabled'. Overrides "
            "the global `settings.run_schedule`; a per-collection `run_schedule:` "
            "overrides this in turn."
        ),
    )
    metadata: str | None = Field(
        default=None,
        description=(
            "Default run schedule for the per-item metadata job. A name in "
            "`settings.run_schedules`, an inline cron, or 'none'/'disabled'. Overrides "
            "the global `settings.run_schedule` for this job."
        ),
    )


class GlobalSettings(BaseModel):
    """Top-level settings applied to all collections unless overridden."""

    model_config = ConfigDict(extra="forbid")

    sync_mode: Literal["append", "sync"] = Field(
        default="append",
        description="Default sync mode: append = add only; sync = also remove items no "
        "longer in the source. Overridable per collection.",
    )
    language: str = Field(
        default="en-US",
        description=(
            "TMDB language tag for text metadata (titles, overviews, genre names): "
            "ISO 639-1, optionally with a region (e.g. en, en-US, en-GB, fr-FR, "
            "pt-BR). Affects TMDB text only -- not artwork selection, and not TVDB "
            "(always English today)."
        ),
    )
    region: str | None = Field(
        default=None,
        description=(
            "TMDB region (ISO 3166-1, e.g. US/GB) -- separate from `language`'s "
            "region suffix; it localises release dates on charts/discover only."
        ),
    )
    hide_year: bool = Field(
        default=True,
        description=(
            "Empty a collection's year + release-date fields (a single year on a "
            "whole collection is meaningless). Overridable per collection."
        ),
    )
    delete_unconfigured_collections: bool = Field(
        default=False,
        description=(
            "On a full run, delete Nalanda-managed collections (ones it previously "
            "created) that are no longer in the config. Never touches collections it "
            "didn't make."
        ),
    )
    unlock_unconfigured_metadata: bool = Field(
        default=False,
        description=(
            "When a per-item metadata entry/field is removed from config, unlock it "
            "in Jellyfin (remove the field lock; clear a managed sort title) so a "
            "refresh can reclaim it. Default false: removed entries are left locked, "
            "like delete_unconfigured_collections."
        ),
    )
    radarr: RadarrDefaults | None = Field(
        default=None,
        description="Global Radarr sync + tagging defaults. Per-collection `radarr:` "
        "blocks override these. Omit if not using Radarr.",
    )
    sonarr: SonarrDefaults | None = Field(
        default=None,
        description="Global Sonarr sync + tagging defaults (the TV analogue). "
        "Per-collection `sonarr:` blocks override these. Omit if not using Sonarr.",
    )
    webhook: WebhookSettings = Field(
        default_factory=WebhookSettings,
        description=(
            "Inbound-request behaviour of the `nalanda serve` daemon (debounce/gating)."
        ),
    )
    cache: CacheSettings = Field(
        default_factory=CacheSettings,
        description="Metadata-cache durations + on/off switch (TMDB/TVDB/MDBList).",
    )
    run_schedules: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Optional named cron strings (e.g. {daily: '0 4 * * *'}), referenced by "
            "name from `run_schedule`, `jobs.*`, or a collection's `run_schedule:`. "
            "Purely for reuse -- a schedule reference may also be an inline cron "
            "string instead of a name."
        ),
    )
    run_schedule: str | None = Field(
        default=None,
        description=(
            "Global default run schedule for every job (level 1 of the cascade): a "
            "name in `run_schedules`, an inline cron, or 'none'/'disabled' = off. "
            "Overridden per job-kind by `jobs.*` and per collection by a collection's "
            "`run_schedule:`. The daemon `serve` reads this; one-off `run` ignores it."
        ),
    )
    jobs: JobSchedules = Field(
        default_factory=JobSchedules,
        description=(
            "Per-job-kind default run schedules (level 2 of the cascade); override "
            "the global `run_schedule` for that kind."
        ),
    )


class SelectFilter(BaseModel):
    """An include/exclude operator mapping, shared by genre, people and company keys.

    ``all`` (AND) / ``any`` (OR) select values to INCLUDE; they are mutually exclusive.
    ``except`` lists values to EXCLUDE -- a filter that drops any title matching one of
    them, and may stand alone (pruning the rest of the block) or accompany ``all`` /
    ``any``. Entries are ids or names (the two may be mixed). At least one must be set.

    Semantics differ by attribute: genres combine server-side (with_genres) and
    except is an attribute filter; people/company combine as client-side set algebra
    (all=intersection, any=union, except=subtract that source's titles).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    all: list[int | str] | None = Field(
        default=None,
        description="Include titles matching ALL of these (AND / intersection).",
    )
    any: list[int | str] | None = Field(
        default=None, description="Include titles matching ANY of these (OR / union)."
    )
    # `except` is a Python keyword, so the field is `excluded` with an `except` alias.
    excluded: list[int | str] | None = Field(
        default=None,
        alias="except",
        description="Exclude any title matching these (a filter, not a source).",
    )

    @model_validator(mode="after")
    def _check(self) -> SelectFilter:
        if self.all is not None and self.any is not None:
            raise ValueError("filter: set only one of 'all' / 'any'")
        if self.all is None and self.any is None and self.excluded is None:
            raise ValueError(
                "filter mapping must set at least one of 'all', 'any', 'except'"
            )
        return self


# MDBList catalog (`/catalog/movie` or `/catalog/show`) sort fields --
# cross-source ratings TMDB lacks.
MdblistCatalogSort = Literal[
    "imdbpopular",
    "imdbrating",
    "imdbvotes",
    "letterrating",
    "metacritic",
    "released",
    "releasedigital",
    "rtaudience",
    "rtomatoes",
    "score",
    "score_average",
    "title",
    "tmdbpopular",
]


class MdblistFilter(BaseModel):
    """One or more MDBList lists, combined and (optionally) sorted/capped.

    ``url`` is one or more list refs (union); ``all`` intersects lists;
    ``except`` subtracts a list's titles. ``sort_by`` is ``"<field>.<asc|desc>"`` (e.g.
    ``imdbrating.desc``); ``limit`` caps each fetch.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    url: str | list[str] | None = Field(
        default=None, description="List URL(s)/ref(s); union."
    )
    all: list[str] | None = Field(
        default=None, description="Only titles in ALL these lists."
    )
    excluded: list[str] | None = Field(
        default=None, alias="except", description="Subtract these lists' titles."
    )
    sort_by: str | None = Field(
        default=None, description='MDBList sort, e.g. "imdbrating.desc" / "rank.asc".'
    )
    limit: int | None = Field(
        default=None, description="Max titles to take from each list."
    )

    @model_validator(mode="after")
    def _check(self) -> MdblistFilter:
        if self.url is not None and self.all is not None:
            raise ValueError("mdblist_list mapping: set only one of 'url' / 'all'")
        if self.url is None and self.all is None and self.excluded is None:
            raise ValueError(
                "mdblist_list mapping must set at least one of 'url', 'all', 'except'"
            )
        return self


class MdblistCatalog(BaseModel):
    """MDBList's discover (`/catalog/movie` or `/catalog/show`) -- filter/sort by
    cross-source ratings."""

    model_config = ConfigDict(extra="forbid")

    genre: list[str] | None = Field(default=None, description="Genre name(s).")
    genre_mode: Literal["and", "or"] = Field(
        default="or", description="Combine genres."
    )
    country: str | None = Field(
        default=None, description="CSV of ISO 3166-1 codes (max 10)."
    )
    language: str | None = Field(
        default=None, description="CSV of ISO 639-1 codes (max 10)."
    )
    score_min: int | None = Field(
        default=None, description="Min MDBList score (0-100)."
    )
    score_max: int | None = Field(
        default=None, description="Max MDBList score (0-100)."
    )
    released_from: str | None = Field(
        default=None, description="Earliest release (YYYY-MM-DD)."
    )
    released_to: str | None = Field(
        default=None, description="Latest release (YYYY-MM-DD)."
    )
    year_min: int | None = None
    year_max: int | None = None
    runtime_min: int | None = None
    runtime_max: int | None = None
    sort: MdblistCatalogSort | None = Field(
        default=None, description="Sort field (e.g. rtomatoes)."
    )
    sort_order: Literal["asc", "desc"] = Field(
        default="desc", description="Sort direction."
    )
    limit: int | None = Field(
        default=None, description="Max titles (default: collection limit)."
    )


class MdblistOfficial(BaseModel):
    """An MDBList official playlist (by slug)."""

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(description="Official playlist slug, e.g. 'popular'.")
    sort_by: str | None = Field(
        default=None, description='MDBList sort, e.g. "score.desc".'
    )
    limit: int | None = None


class BuilderBlock(BaseModel):
    """One ordered block of sources.

    A block is resolved in its natural order (a sole single curated list keeps its
    order; anything else is release-date sorted). Blocks are concatenated in order via
    ``CollectionDef.append`` so the user can control cross-block ordering.
    """

    model_config = ConfigDict(extra="forbid")

    match: Literal["any", "all"] = Field(
        default="any",
        description=(
            "How the block's keys combine: any (union, default) | all (intersection "
            "-- a title must match EVERY key)."
        ),
    )
    tmdb_collection: int | list[int] | None = Field(
        default=None,
        description=(
            "TMDB collection id(s); members are merged and sorted chronologically."
        ),
    )
    tmdb_movie: int | list[int] | None = Field(
        default=None, description="Individual TMDB movie id(s)."
    )
    tmdb_title: str | int | list[str | int] | None = Field(
        default=None,
        description=(
            'Title(s) resolved via search, e.g. "A Film Title" or '
            '"A Remade Title (1999)" to disambiguate by year. Numeric titles may be '
            "unquoted."
        ),
    )
    tmdb_keyword: int | str | list[int | str] | SelectFilter | None = Field(
        default=None,
        description=(
            "TMDB keyword id(s)/name(s), or {all|any|except: [...]}. A Discover "
            "query (bare list = OR), joined chronologically."
        ),
    )
    tmdb_genre: int | str | list[int | str] | SelectFilter | None = Field(
        default=None,
        description=(
            "Genre id/name, a bare list (AND), or a {all|any|except: [...]} mapping. "
            "A Discover query, joined chronologically."
        ),
    )
    # People builders (TMDB person id or name; joined chronologically). actor = acting
    # credits; director/writer/producer = crew filtered by department; crew = all crew.
    # A list or {any} = union; {all} = titles with EVERY person; {except} = exclude
    # theirs.
    tmdb_actor: int | str | list[int | str] | SelectFilter | None = Field(
        default=None,
        description="TMDB person id(s) or name(s); their acting credits.",
    )
    tmdb_director: int | str | list[int | str] | SelectFilter | None = Field(
        default=None,
        description="TMDB person id(s) or name(s); titles they directed.",
    )
    tmdb_writer: int | str | list[int | str] | SelectFilter | None = Field(
        default=None, description="TMDB person id(s) or name(s); titles they wrote."
    )
    tmdb_producer: int | str | list[int | str] | SelectFilter | None = Field(
        default=None, description="TMDB person id(s) or name(s); titles they produced."
    )
    tmdb_crew: int | str | list[int | str] | SelectFilter | None = Field(
        default=None,
        description="TMDB person id(s) or name(s); all their crew credits.",
    )
    tmdb_company: int | str | list[int | str] | SelectFilter | None = Field(
        default=None,
        description="TMDB production company id(s) or name(s); joined chronologically.",
    )
    # Chart builders -- the value is the COUNT of titles to take, in the chart's order.
    tmdb_popular: int | None = Field(
        default=None, description="Top N of TMDB's Popular chart."
    )
    tmdb_now_playing: int | None = Field(
        default=None, description="Top N of TMDB's Now Playing chart."
    )
    tmdb_top_rated: int | None = Field(
        default=None, description="Top N of TMDB's Top Rated chart."
    )
    tmdb_upcoming: int | None = Field(
        default=None, description="Top N of TMDB's Upcoming chart."
    )
    tmdb_trending_daily: int | None = Field(
        default=None, description="Top N of TMDB's daily Trending chart."
    )
    tmdb_trending_weekly: int | None = Field(
        default=None, description="Top N of TMDB's weekly Trending chart."
    )
    # --- TV-only source keys (media: tv) ---
    # The show analogues of the movie keys above. `tmdb_show` mirrors `tmdb_movie`;
    # `tmdb_network` is TV's `tmdb_company`; `on_the_air`/`airing_today` are TV charts.
    # The shared keys (tmdb_title/keyword/genre/people/company/discover/list and the
    # popular/top_rated/trending charts) work for BOTH and dispatch on the
    # collection's `media`.
    tmdb_show: int | list[int] | None = Field(
        default=None,
        description="Individual TMDB show (TV) id(s). The tv analogue of tmdb_movie.",
    )
    tmdb_network: int | str | list[int | str] | SelectFilter | None = Field(
        default=None,
        description=(
            "TMDB TV network id(s), or {all|any|except: [...]}. The tv analogue of "
            "tmdb_company. Networks must be given by id (TMDB has no network name "
            "search)."
        ),
    )
    tmdb_on_the_air: int | None = Field(
        default=None, description="Top N of TMDB's TV On The Air chart (tv only)."
    )
    tmdb_airing_today: int | None = Field(
        default=None, description="Top N of TMDB's TV Airing Today chart (tv only)."
    )
    # Escape hatch: raw TMDB Discover params (with_*/without_*, vote/date ranges,
    # sort_by). `limit` and `page` are intercepted; everything else passes through.
    # Kept in sort order.
    tmdb_discover: dict[str, Any] | None = Field(
        default=None,
        description="Raw TMDB Discover query (escape hatch); `limit` is honoured.",
    )
    tmdb_list: int | list[int] | SelectFilter | None = Field(
        default=None,
        description=(
            "TMDB list id(s), or {all|any|except: [...]} (intersection/union/subtract "
            "of lists); kept in curated order."
        ),
    )
    mdblist_list: str | list[str] | MdblistFilter | None = Field(
        default=None,
        description=(
            "MDBList list URL(s)/ref(s), or {url|all|except, sort_by, limit}; curated."
        ),
    )
    mdblist_catalog: MdblistCatalog | None = Field(
        default=None,
        description=(
            "MDBList discover -- filter/sort by cross-source ratings"
            " (RT/Metacritic/...)."
        ),
    )
    mdblist_official: str | MdblistOfficial | None = Field(
        default=None,
        description=(
            "MDBList official playlist by slug (e.g. 'popular'), or"
            " {slug, sort_by, limit}."
        ),
    )
    # --- TVDB source keys (no key needed; Nalanda ships a per-project TVDB key) ---
    # TVDB ids are Sonarr-native. `tvdb_show`/`tvdb_movie` take id(s), slug(s) or
    # name(s); `tvdb_list`/`tvdb_discover` are media-dispatched (filtered to the
    # collection's media).
    tvdb_show: int | str | list[int | str] | None = Field(
        default=None, description="TVDB series id(s)/slug(s)/name(s) (tv only)."
    )
    tvdb_movie: int | str | list[int | str] | None = Field(
        default=None,
        description=(
            "TVDB movie id(s)/slug(s)/name(s) (movie only); resolved to TMDB/IMDb ids."
        ),
    )
    tvdb_list: int | str | None = Field(
        default=None,
        description=(
            "A TVDB list id or slug; its items of the collection's media,"
            " curated order."
        ),
    )
    tvdb_discover: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Raw TVDB filter query (escape hatch: "
            "genre/company/country/lang/year/sort); "
            "`limit` is honoured. country+lang default to usa/eng."
        ),
    )


# Builder keys valid for only one media type (checked per block by
# CollectionDef._check_media).
MOVIE_ONLY_BUILDERS = (
    "tmdb_collection",
    "tmdb_movie",
    "tmdb_now_playing",
    "tmdb_upcoming",
    "tvdb_movie",
)
TV_ONLY_BUILDERS = (
    "tmdb_show",
    "tmdb_network",
    "tmdb_on_the_air",
    "tmdb_airing_today",
    "tvdb_show",
)


class CollectionDef(BuilderBlock):
    """A single collection definition.

    The top-level builder keys form the first block; ``append`` adds further ordered
    blocks after it. Unknown keys are rejected so typos surface immediately.
    """

    model_config = ConfigDict(extra="forbid")

    media: Literal["movie", "tv", "mixed"] = Field(
        description=(
            "What this collection builds: movie (Radarr / Jellyfin movie libraries), "
            "tv (Sonarr / Jellyfin show libraries), or mixed (one BoxSet holding both, "
            "driving Radarr AND Sonarr). Selects the client(s), library type(s), and "
            "the movie-vs-TV dialect of shared builder keys -- in a mixed collection a "
            "shared key (genre, people, charts, mdblist, ...) produces BOTH movies and "
            "shows. Required."
        ),
    )

    # Ordered builder blocks appended after the top-level (first) block.
    append: list[BuilderBlock] | None = Field(
        default=None,
        description=(
            "Further ordered blocks, each resolved independently and concatenated "
            "after the first block."
        ),
    )

    overview: str | None = Field(
        default=None,
        description="Overview text for the collection; overrides any source metadata.",
    )
    tmdb_overview: int | None = Field(
        default=None,
        description=(
            "TMDB collection id whose overview is fetched at runtime and used as the "
            "collection overview -- for merges, where there is no sole source to "
            "auto-fill from. A literal `overview` overrides it."
        ),
    )
    sort_title: str | None = Field(
        default=None, description="Sort title used to position the collection itself."
    )
    # Per-slot artwork overrides. Each slot takes a remote URL (Jellyfin downloads it)
    # OR a local file path (its bytes are uploaded) -- never both (see `_check_art`).
    # Either wins over the artwork repo and the auto-sourced TMDB image for that slot.
    # Omit both to fall through to the repo, then TMDB.
    primary_art_url: str | None = Field(
        default=None,
        description=(
            "Primary (poster) image URL; Jellyfin downloads it. Overrides the "
            "artwork repo and the auto-sourced TMDB poster. Exclusive with "
            "`primary_art_file`."
        ),
    )
    primary_art_file: str | None = Field(
        default=None,
        description="Path to a local Primary (poster) image; its bytes are uploaded. "
        "Exclusive with `primary_art_url`.",
    )
    thumb_art_url: str | None = Field(
        default=None,
        description=(
            "Thumb (landscape title card) image URL; Jellyfin downloads it. "
            "Overrides the artwork repo and the auto-sourced TMDB titled backdrop. "
            "Exclusive with `thumb_art_file`."
        ),
    )
    thumb_art_file: str | None = Field(
        default=None,
        description="Path to a local Thumb (landscape title card) image; its bytes are "
        "uploaded. Exclusive with `thumb_art_url`.",
    )
    backdrop_art_url: str | None = Field(
        default=None,
        description="Backdrop (fanart) image URL; Jellyfin downloads it. Overrides the "
        "artwork repo and the auto-sourced TMDB textless backdrop. Exclusive with "
        "`backdrop_art_file`.",
    )
    backdrop_art_file: str | None = Field(
        default=None,
        description="Path to a local Backdrop (fanart) image; its bytes are uploaded. "
        "Exclusive with `backdrop_art_url`.",
    )
    sync_mode: Literal["append", "sync"] | None = Field(
        default=None, description="Per-collection override of the global sync_mode."
    )
    hide_year: bool | None = Field(
        default=None,
        description=(
            "Override the global hide_year for this collection (empty its year + "
            "release-date fields). Unset = use the global setting."
        ),
    )
    run_schedule: str | None = Field(
        default=None,
        description=(
            "Per-collection run-schedule override (level 3, the most specific): a "
            "name in `settings.run_schedules`, an inline cron, or 'none'/'disabled' "
            "to never schedule it. Unset inherits `settings.jobs.collections`, then "
            "`settings.run_schedule`. Only the daemon `serve` schedules; this "
            "collection still runs on demand via webhook or manual `run`."
        ),
    )
    order: Literal["source", "sort_name", "release_date"] | None = Field(
        default=None,
        description=(
            "Display order: source (as built, DisplayOrder=Default) | sort_name | "
            "release_date. When unset, defaults to release_date if the collection is "
            "release-sorted at build (single/merged collections, query builders) or "
            "source for a sole curated list (charts, tmdb_list, mdblist_list)."
        ),
    )
    limit: int | None = Field(
        default=None,
        description=(
            "Cap each broad query builder (genre/keyword/company/people/discover) to "
            "this many titles, most-popular-first. Defaults to 100; set 0 for "
            "unlimited."
        ),
    )
    libraries: list[str] | None = Field(
        default=None,
        description=(
            "Jellyfin libraries (by name) to match within; omit for all libraries of "
            "the collection's media type (movie or tv)."
        ),
    )
    section: str | None = Field(
        default=None,
        description="Name of a top-level `sections` entry. Gives the collection an "
        "auto-generated sort title so sections group together, in the order declared.",
    )
    radarr: RadarrOptions | None = Field(
        default=None,
        description=(
            "Radarr sync + identity-tag options. Set `enable: true` to opt the "
            "collection into Radarr management (the block is inert otherwise); other "
            "fields inherit the global `settings.radarr` defaults. `movie` or `mixed` "
            "collections only."
        ),
    )
    sonarr: SonarrOptions | None = Field(
        default=None,
        description=(
            "Sonarr sync + identity-tag options (the TV analogue of `radarr:`). "
            "Set `enable: true` to opt the collection into Sonarr management (inert "
            "otherwise); other fields inherit the global `settings.sonarr` defaults. "
            "`tv` or `mixed` only."
        ),
    )

    @model_validator(mode="after")
    def _check_media(self) -> CollectionDef:
        """Reject options/builder keys that don't match the collection's media type.

        Radarr drives movies, Sonarr drives shows; some builder keys exist for only one
        media. A `mixed` collection drives both and accepts both arr blocks and both key
        families. Every block (top-level + each `append`) is checked.
        """
        if self.media == "tv" and self.radarr is not None:
            raise ValueError(
                "`radarr:` is for movie/mixed collections; use `sonarr:` on a "
                "tv collection"
            )
        if self.media == "movie" and self.sonarr is not None:
            raise ValueError(
                "`sonarr:` is for tv/mixed collections; use `radarr:` on a "
                "movie collection"
            )
        # tmdb_overview fetches a *movie* collection's overview (generic text), so it is
        # valid on movie and mixed collections and rejected only on a pure-tv one.
        if self.media == "tv" and self.tmdb_overview is not None:
            raise ValueError(
                "`tmdb_overview` fetches a TMDB *movie* collection overview; "
                "not valid on a tv collection"
            )
        # mixed accepts both key families; movie/tv reject the other's
        # media-specific keys.
        if self.media == "movie":
            wrong: tuple[str, ...] = TV_ONLY_BUILDERS
        elif self.media == "tv":
            wrong = MOVIE_ONLY_BUILDERS
        else:  # mixed
            wrong = ()
        for block in [self, *(self.append or [])]:
            used = [k for k in wrong if getattr(block, k) is not None]
            if used:
                raise ValueError(
                    f"builder key(s) {sorted(used)} are not valid for a "
                    f"{self.media!r} collection"
                )
        return self

    @model_validator(mode="after")
    def _check_art(self) -> CollectionDef:
        """Each art slot takes a URL or a file, never both
        (`*_art_url` xor `*_art_file`)."""
        for slot in ("primary", "thumb", "backdrop"):
            if getattr(self, f"{slot}_art_url") and getattr(self, f"{slot}_art_file"):
                raise ValueError(
                    f"{slot} art: set only one of '{slot}_art_url' / '{slot}_art_file'"
                )
        return self


class ArtworkRepo(BaseModel):
    """A local artwork repository -- per-collection images dropped on disk.

    For each collection, Nalanda looks for
    ``<path>/collections/<slug>/<type>.<ext>`` where ``<type>`` is ``primary`` /
    ``thumb`` / ``backdrop``, ``<ext>`` is ``png`` / ``jpg`` / ``jpeg`` / ``webp``,
    and ``<slug>`` is the collection name slugified (the same slug used for identity
    tags: lowercased, runs of non-alphanumerics hyphenated -- so "A Title:
    Subtitle" -> "a-title-subtitle"). A found file is uploaded and its bytes hashed
    so an unchanged file isn't re-uploaded. A collection's per-slot
    ``*_art_url`` / ``*_art_file`` overrides the repo.
    """

    model_config = ConfigDict(extra="forbid")

    path: str | None = Field(
        default=None,
        description="Filesystem root of the artwork repository. Nalanda reads "
        "`<path>/collections/<slug>/<primary|thumb|backdrop>.<png|jpg|jpeg|webp>`.",
    )
    create_empty_folders: bool = Field(
        default=False,
        description="Create an empty `<path>/collections/<slug>/` folder for every "
        "configured collection, giving you a place to drop artwork.",
    )
    delete_old_empty_folders: bool = Field(
        default=False,
        description=(
            "Delete *empty* `<path>/collections/<slug>/` folders whose collection "
            "is no longer configured. A folder containing any file is never removed."
        ),
    )


# Per-item metadata override fields (config key -> Jellyfin field). Only individually
# lockable Jellyfin fields, plus sort_title (sticky via ForcedSortName).
# See metadata.py.
METADATA_FIELDS = ("parental_rating", "title", "overview", "sort_title")


class MetadataEntry(BaseModel):
    """One item's metadata overrides, keyed by a single provider id.

    Exactly one of ``tmdb`` / ``imdb`` / ``tvdb`` identifies the item (movies are
    normally tmdb/imdb, shows tvdb/tmdb/imdb); at least one override field must be
    set. Each declared field is written to Jellyfin; the lockable fields
    (parental_rating, title, overview) are locked so a metadata refresh cannot
    overwrite them, and sort_title persists via ForcedSortName (re-asserted each run).
    """

    model_config = ConfigDict(extra="forbid")

    tmdb: int | None = None
    imdb: str | None = None
    tvdb: int | None = None
    parental_rating: str | int | None = Field(
        default=None,
        description=(
            "Jellyfin Parental Rating (OfficialRating), e.g. GB-15 or 15. Written and "
            "locked. A bare number (a UK/AU rating) is accepted and stored as a string."
        ),
    )
    title: str | int | None = Field(
        default=None, description="Item title (Name). Written and locked."
    )
    overview: str | int | None = Field(
        default=None, description="Item overview. Written and locked."
    )
    sort_title: str | int | None = Field(
        default=None,
        description=(
            "Sort Title (ForcedSortName). Sticky across normal refreshes; "
            "Nalanda re-asserts it each run, but a replace-all-metadata refresh in "
            "Jellyfin can still clear it."
        ),
    )

    @field_validator(
        "parental_rating", "title", "overview", "sort_title", mode="before"
    )
    @classmethod
    def _stringify_numbers(cls, v: Any) -> Any:
        # YAML parses bare numbers (UK/AU ratings like 15, numeric titles like 2000) as
        # ints; Jellyfin stores these as strings, so accept a number and stringify it.
        # bool is an int subclass, so guard against `true`/`false` slipping through.
        if isinstance(v, bool):
            return v
        return str(v) if isinstance(v, int) else v

    @model_validator(mode="after")
    def _check(self) -> MetadataEntry:
        ids = [v for v in (self.tmdb, self.imdb, self.tvdb) if v is not None]
        if len(ids) != 1:
            raise ValueError("metadata entry: set exactly one of tmdb / imdb / tvdb")
        if not self.field_values():
            raise ValueError(
                "metadata entry: set at least one of " + ", ".join(METADATA_FIELDS)
            )
        return self

    @property
    def provider_key(self) -> str:
        """Stable id key for the state file, e.g. ``tmdb:1234`` / ``tvdb:5678``."""
        if self.tmdb is not None:
            return f"tmdb:{self.tmdb}"
        if self.tvdb is not None:
            return f"tvdb:{self.tvdb}"
        return f"imdb:{self.imdb}"

    def field_values(self) -> dict[str, str]:
        """The set override fields as ``{config key: value}``."""
        return {k: v for k in METADATA_FIELDS if (v := getattr(self, k)) is not None}


class MetadataConfig(BaseModel):
    """Per-item metadata overrides, grouped by media so id-to-library routing is
    unambiguous."""

    model_config = ConfigDict(extra="forbid")

    movies: list[MetadataEntry] = Field(
        default_factory=list,
        description=(
            "Per-movie metadata overrides, normally keyed by tmdb or imdb id "
            "(any one provider id is accepted)."
        ),
    )
    shows: list[MetadataEntry] = Field(
        default_factory=list,
        description=(
            "Per-series metadata overrides, normally keyed by tvdb, or tmdb/imdb "
            "(any one provider id is accepted)."
        ),
    )


class Config(BaseModel):
    """The parsed ``config.yml``."""

    model_config = ConfigDict(extra="forbid")

    settings: GlobalSettings = Field(default_factory=GlobalSettings)
    sections: list[str] = Field(
        default_factory=list,
        description="Ordered section names. A collection's `section` gives it a sort "
        "title prefixed by the section's position here, so sections group together in "
        "this order in the Jellyfin library view.",
    )
    collections: dict[str, CollectionDef] = Field(default_factory=dict)
    artwork_repo: ArtworkRepo | None = Field(
        default=None,
        description="Optional local artwork repository: per-collection images on disk, "
        "found by slugified name under `<path>/collections/`.",
    )
    metadata: MetadataConfig | None = Field(
        default=None,
        description="Per-item metadata overrides written and locked into Jellyfin "
        "(parental_rating, title, overview, sort_title), grouped by media.",
    )

    @model_validator(mode="after")
    def _check_artwork_slugs(self) -> Config:
        """With an artwork repo configured, every collection must map to a distinct
        folder slug -- two names slugifying the same would share (and clobber) one
        artwork folder."""
        if not (self.artwork_repo and self.artwork_repo.path):
            return self
        seen: dict[str, str] = {}
        for cname in self.collections:
            slug = slugify(cname)
            if slug in seen:
                raise ValueError(
                    f"collections {seen[slug]!r} and {cname!r} both slugify to "
                    f"{slug!r}; their artwork folders would collide -- rename one"
                )
            seen[slug] = cname
        return self

    @model_validator(mode="after")
    def _sections_exist(self) -> Config:
        for cname, coll in self.collections.items():
            if coll.section is not None and coll.section not in self.sections:
                raise ValueError(
                    f"collection {cname!r}: section {coll.section!r} is not in the "
                    f"top-level `sections` list {self.sections}"
                )
        return self

    @model_validator(mode="after")
    def _schedules_valid(self) -> Config:
        """Every schedule reference must be a known name, a valid cron, or an off
        sentinel.

        Validated at load (not daemon start) so a typo'd cron or dangling name surfaces
        immediately, like unknown keys do.
        """
        names = set(self.settings.run_schedules)
        for label, expr in self.settings.run_schedules.items():
            if not croniter.is_valid(expr):
                raise ValueError(
                    f"settings.run_schedules[{label!r}] = {expr!r} is not a valid "
                    "cron expression"
                )

        def _check(ref: str | None, where: str) -> None:
            if ref is None or _is_schedule_off(ref) or ref in names:
                return
            if not croniter.is_valid(ref):
                raise ValueError(
                    f"{where} = {ref!r} is neither a name in settings.run_schedules "
                    f"{sorted(names)} nor a valid cron expression"
                )

        _check(self.settings.run_schedule, "settings.run_schedule")
        _check(self.settings.jobs.collections, "settings.jobs.collections")
        _check(self.settings.jobs.metadata, "settings.jobs.metadata")
        for cname, coll in self.collections.items():
            _check(coll.run_schedule, f"collection {cname!r} run_schedule")
        return self

    def _resolve_schedule_ref(self, ref: str | None) -> str | None:
        """A validated schedule reference -> its cron string, or ``None`` if
        unset/off."""
        if ref is None or _is_schedule_off(ref):
            return None
        return self.settings.run_schedules.get(ref, ref)

    def resolve_job_cron(self, kind: str) -> str | None:
        """The single resolved cron for a monolithic job (``settings.jobs.<kind>``, else
        ``settings.run_schedule``), or ``None`` if unset or an off sentinel.

        Use for jobs that have one schedule for the whole job (e.g. metadata). For
        per-collection scheduling use :meth:`resolve_schedules` instead.
        """
        if kind not in JobSchedules.model_fields:
            raise ValueError(f"unknown job kind: {kind!r}")
        ref = getattr(self.settings.jobs, kind)
        if ref is None:
            ref = self.settings.run_schedule
        return self._resolve_schedule_ref(ref)

    def resolve_schedules(self, kind: str) -> tuple[dict[str, set[str]], str | None]:
        """Resolve the schedule cascade for one job ``kind`` (today only
        ``collections``).

        Returns ``(groups, default_cron)``:

        * ``groups`` maps each distinct cron to the set of collections that fire on it.
          A collection's schedule is its own ``run_schedule:`` if set, else the
          job-kind default (``settings.jobs.<kind>``), else the global
          ``settings.run_schedule``; an off sentinel (or no match at any level) leaves
          it unscheduled and out of every group.
        * ``default_cron`` is the resolved cron of the job-kind/global default, or
          ``None``. The run on this cron also prunes (orphaned collections + empty art
          folders), so the caller fires it as a pruning run; with ``None`` no scheduled
          run prunes.
        """
        default_ref = getattr(self.settings.jobs, kind, None)
        if default_ref is None:
            default_ref = self.settings.run_schedule
        default_cron = self._resolve_schedule_ref(default_ref)

        groups: dict[str, set[str]] = {}
        for name, coll in self.collections.items():
            cron = (
                self._resolve_schedule_ref(coll.run_schedule)
                if coll.run_schedule is not None
                else default_cron
            )
            if cron is not None:
                groups.setdefault(cron, set()).add(name)
        return groups, default_cron


def load_config(path: str | Path = "config.yml") -> Config:
    """Read and validate the YAML collection config."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {p}")
    raw: Any = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return Config.model_validate(raw)


def json_schema() -> dict[str, Any]:
    """The JSON Schema for ``config.yml``, generated from these models.

    Generated -- never hand-written -- so editor validation can never drift from what
    the code actually accepts. Reference it from the top of your config:

        # yaml-language-server: $schema=./config.schema.json

    Regenerate with ``python -m nalanda schema``.
    """
    schema = Config.model_json_schema(by_alias=True)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **schema,
        "title": "Nalanda configuration",
    }
