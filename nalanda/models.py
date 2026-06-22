"""Core domain models, shared across clients and the collection pipeline."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

MediaType = Literal["movie", "tv"]


@dataclass(frozen=True)
class ImageSource:
    """A resolved artwork source for one Jellyfin image slot (Primary/Thumb/Backdrop).

    ``kind`` selects how it is applied: a remote ``url`` Jellyfin downloads itself, or
    a local ``file`` whose bytes we upload. ``marker`` is the idempotency key stored
    in the state file and compared on the next run -- the URL for a remote image,
    ``file:<sha256>`` for a local one, so editing a file's bytes (under a stable path)
    still re-uploads while an unchanged run stays a no-op. ``content_type`` carries
    the upload MIME for files.
    """

    kind: Literal["url", "file"]
    ref: str  # the remote URL, or the local filesystem path
    marker: str  # comparison key: the URL, or "file:<sha256>"
    content_type: str | None = None  # upload MIME for files; None for remote URLs

    @classmethod
    def from_url(cls, url: str) -> ImageSource:
        return cls(kind="url", ref=url, marker=url)


@dataclass(frozen=True)
class ArtCandidate:
    """A provider-agnostic artwork candidate for one Jellyfin slot.

    Adapters (TMDB today; TVDB later) map their raw artwork onto these, and the shared
    selector in :mod:`nalanda.art_select` ranks them per slot identically regardless
    of source. ``has_text`` is the unifier -- the titled/textless signal (TMDB infers
    it from a language tag; TVDB has an explicit ``includesText``). ``score`` is the
    provider's own rank key (e.g. rating, vote count), applied after the language
    tier.
    """

    slot: Literal["Primary", "Thumb", "Backdrop"]
    path: str
    lang: str | None = None  # canonical 2-letter subtag; None = textless
    has_text: bool = False  # True = titled art (Jellyfin Thumb)
    score: tuple[float, ...] = ()  # provider rank key, applied after the language tier


def _year_from_release_date(value: Any) -> int | None:
    text = str(value or "")
    return int(text[:4]) if text[:4].isdigit() else None


class MediaItem(BaseModel):
    """A movie or show, normalised from whatever source produced it (TMDB, TVDB,
    MDBList, ...).

    The same shape serves both: movies key on ``tmdb_id``, shows on ``tvdb_id``
    (falling back to ``tmdb_id``/``imdb_id``). ``media_type`` records which a given
    item is.
    """

    tmdb_id: int | None = None
    imdb_id: str | None = None
    tvdb_id: int | None = (
        None  # primary id for shows (Sonarr-native); also set for TVDB movies
    )
    media_type: MediaType = "movie"
    title: str
    year: int | None = None
    release_date: str | None = None  # full ISO date, for precise chronological sorting
    overview: str | None = None
    poster_path: str | None = None
    genre_ids: list[int] = Field(
        default_factory=list
    )  # TMDB genre ids, for exclusion filters
    popularity: float = 0.0  # TMDB popularity, for `limit` (most-popular-first)
    ratings: list[dict[str, Any]] | None = (
        None  # MDBList cross-source ratings, when appended
    )

    def identity_keys(self) -> tuple[tuple[str, str, int | str], ...]:
        """Media-scoped provider id keys (tmdb / tvdb / imdb) for identity matching.

        Two items refer to the same title if any of their keys match -- the same "any
        shared provider id" rule :func:`builders.dedupe_movies` uses. Scoped by
        ``media_type`` so a movie and a show that happen to share a numeric id never
        collide. An item with no ids returns an empty tuple (it can't be matched
        against anything).
        """
        keys: list[tuple[str, str, int | str]] = []
        if self.tmdb_id is not None:
            keys.append((self.media_type, "tmdb", self.tmdb_id))
        if self.tvdb_id is not None:
            keys.append((self.media_type, "tvdb", self.tvdb_id))
        if self.imdb_id:
            keys.append((self.media_type, "imdb", self.imdb_id))
        return tuple(keys)

    @classmethod
    def from_tmdb(cls, data: dict[str, Any]) -> MediaItem:
        """Build from a TMDB movie object (details, collection part, or list item)."""
        imdb_id = data.get("imdb_id")
        external = data.get("external_ids")
        if not imdb_id and isinstance(external, dict):
            imdb_id = external.get("imdb_id")
        # Discover/list/collection items carry `genre_ids`; full details carry `genres`.
        genre_ids = data.get("genre_ids")
        if genre_ids is None:
            genres = data.get("genres")
            genre_ids = (
                [g["id"] for g in genres if g.get("id") is not None] if genres else []
            )
        return cls(
            tmdb_id=data.get("id"),
            imdb_id=imdb_id or None,
            title=data.get("title") or data.get("name") or "?",
            year=_year_from_release_date(data.get("release_date")),
            release_date=data.get("release_date") or None,
            overview=data.get("overview") or None,
            poster_path=data.get("poster_path") or None,
            genre_ids=[int(g) for g in genre_ids],
            popularity=float(data.get("popularity") or 0.0),
        )

    @classmethod
    def from_tmdb_tv(cls, data: dict[str, Any]) -> MediaItem:
        """Build from a TMDB **TV** object (uses ``name``/``first_air_date``).

        TV shapes differ from movies: the title is ``name``, the date is
        ``first_air_date``, and ``external_ids`` (when appended) carries
        ``tvdb_id``/``imdb_id`` -- the ids Sonarr and Jellyfin's TV provider key on.
        """
        external = data.get("external_ids")
        external = external if isinstance(external, dict) else {}
        tvdb_id = external.get("tvdb_id")
        imdb_id = external.get("imdb_id")
        genre_ids = data.get("genre_ids")
        if genre_ids is None:
            genres = data.get("genres")
            genre_ids = (
                [g["id"] for g in genres if g.get("id") is not None] if genres else []
            )
        return cls(
            tmdb_id=data.get("id"),
            imdb_id=imdb_id or None,
            tvdb_id=int(tvdb_id) if tvdb_id else None,
            media_type="tv",
            title=data.get("name") or data.get("title") or "?",
            year=_year_from_release_date(data.get("first_air_date")),
            release_date=data.get("first_air_date") or None,
            overview=data.get("overview") or None,
            poster_path=data.get("poster_path") or None,
            genre_ids=[int(g) for g in genre_ids],
            popularity=float(data.get("popularity") or 0.0),
        )

    @classmethod
    def from_mdblist(
        cls,
        data: dict[str, Any],
        *,
        genre_resolver: Callable[[str], int | None] | None = None,
        media_type: MediaType = "movie",
    ) -> MediaItem:
        """Build from an MDBList API item (movie or show).

        Handles both shapes: list/official items use ``ids.tmdb``/``release_year`` and
        (when appended) carry ``genres`` (slugs), ``release_date``, ``description``,
        ``ratings``; catalog items use ``ids.tmdbid``/``year``. Show items additionally
        carry ``ids.tvdb``/``tvdbid``. ``genre_resolver`` maps genre slugs -> TMDB genre
        ids (unmapped ones are dropped).
        """
        ids = data.get("ids") or {}
        genre_ids: list[int] = []
        if genre_resolver:
            for slug in data.get("genres") or []:
                gid = genre_resolver(slug)
                if gid is not None:
                    genre_ids.append(gid)
        tvdb_id = ids.get("tvdb") or ids.get("tvdbid")
        return cls(
            tmdb_id=ids.get("tmdb") or ids.get("tmdbid") or data.get("id") or None,
            imdb_id=ids.get("imdb") or ids.get("imdbid") or data.get("imdb_id") or None,
            tvdb_id=int(tvdb_id) if tvdb_id else None,
            media_type=media_type,
            title=data.get("title") or "?",
            year=data.get("release_year") or data.get("year") or None,
            release_date=data.get("release_date") or None,
            overview=data.get("description") or None,
            genre_ids=genre_ids,
            ratings=data.get("ratings") or None,
        )


class MovieCollection(BaseModel):
    """A set of movies plus the metadata describing the collection itself."""

    tmdb_id: int
    name: str
    overview: str | None = None
    poster_path: str | None = None
    backdrop_path: str | None = None  # textless backdrop -> Jellyfin Backdrop
    thumb_path: str | None = None  # titled (language) backdrop -> Jellyfin Thumb
    movies: list[MediaItem] = Field(default_factory=list)


class JellyfinLibrary(BaseModel):
    """A Jellyfin media library (virtual folder)."""

    id: str
    name: str
    collection_type: str | None = None  # "movies", "tvshows", ...

    @classmethod
    def from_jellyfin(cls, data: dict[str, Any]) -> JellyfinLibrary:
        return cls(
            id=data.get("ItemId") or "",
            name=data.get("Name") or "?",
            collection_type=data.get("CollectionType"),
        )


class JellyfinItem(BaseModel):
    """A library item in Jellyfin, with its external provider ids.

    ``provider_ids`` keeps Jellyfin's PascalCase keys verbatim (``Tmdb``,
    ``Imdb``, ``Tvdb``, ...) so matching can use whichever ids an item carries.
    """

    id: str
    name: str
    year: int | None = None
    type: str | None = None
    provider_ids: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_jellyfin(cls, data: dict[str, Any]) -> JellyfinItem:
        raw = data.get("ProviderIds") or {}
        return cls(
            id=data.get("Id") or "",
            name=data.get("Name") or "?",
            year=data.get("ProductionYear"),
            type=data.get("Type"),
            provider_ids={k: str(v) for k, v in raw.items() if v is not None},
        )


class JellyfinCollection(BaseModel):
    """An existing Jellyfin collection (BoxSet), as discovered on the server."""

    id: str
    name: str
    child_count: int | None = None
    display_order: str | None = None

    @classmethod
    def from_jellyfin(cls, data: dict[str, Any]) -> JellyfinCollection:
        return cls(
            id=data.get("Id") or "",
            name=data.get("Name") or "?",
            child_count=data.get("ChildCount"),
            display_order=data.get("DisplayOrder"),
        )


class ArrQualityProfile(BaseModel):
    """A quality profile as Radarr or Sonarr returns it."""

    id: int
    name: str

    @classmethod
    def from_arr(cls, data: dict[str, Any]) -> ArrQualityProfile:
        return cls(id=data["id"], name=data.get("name") or "?")


class ArrTag(BaseModel):
    """An identity tag as Radarr or Sonarr returns it (ids resolve labels)."""

    id: int
    label: str

    @classmethod
    def from_arr(cls, data: dict[str, Any]) -> ArrTag:
        return cls(id=data["id"], label=data.get("label") or "?")


class ArrRootFolder(BaseModel):
    """A configured root folder as Radarr or Sonarr returns it."""

    id: int | None = None
    path: str
    accessible: bool | None = None

    @classmethod
    def from_arr(cls, data: dict[str, Any]) -> ArrRootFolder:
        return cls(
            id=data.get("id"),
            path=data.get("path") or "",
            accessible=data.get("accessible"),
        )


# Radarr and Sonarr expose these three resources identically -- one shape, aliased.
RadarrQualityProfile = SonarrQualityProfile = ArrQualityProfile
RadarrTag = SonarrTag = ArrTag
RadarrRootFolder = SonarrRootFolder = ArrRootFolder


class RadarrMovie(BaseModel):
    """A movie as Radarr knows it. ``tags`` are tag ids (resolve via RadarrTag)."""

    id: int | None = None  # None for lookup results not yet in the library
    tmdb_id: int | None = None
    imdb_id: str | None = None
    title: str
    year: int | None = None
    quality_profile_id: int | None = None
    monitored: bool = False
    has_file: bool = False
    tags: list[int] = Field(default_factory=list)

    @classmethod
    def from_radarr(cls, data: dict[str, Any]) -> RadarrMovie:
        return cls(
            id=data.get("id") or None,  # Radarr returns id 0 for not-yet-added lookups
            tmdb_id=data.get("tmdbId"),
            imdb_id=data.get("imdbId") or None,
            title=data.get("title") or "?",
            year=data.get("year") or None,
            quality_profile_id=data.get("qualityProfileId"),
            monitored=bool(data.get("monitored")),
            has_file=bool(data.get("hasFile")),
            tags=list(data.get("tags") or []),
        )


# --- Sonarr (TV) -- the show-side analogues of the Radarr resources above. -----------


class SonarrLanguageProfile(BaseModel):
    """A Sonarr v3 language profile (removed in v4 -- the endpoint may 404)."""

    id: int
    name: str

    @classmethod
    def from_sonarr(cls, data: dict[str, Any]) -> SonarrLanguageProfile:
        return cls(id=data["id"], name=data.get("name") or "?")


class SonarrSeason(BaseModel):
    """One season's monitor state within a series."""

    season_number: int
    monitored: bool = False

    @classmethod
    def from_sonarr(cls, data: dict[str, Any]) -> SonarrSeason:
        return cls(
            season_number=data.get("seasonNumber") or 0,
            monitored=bool(data.get("monitored")),
        )


class SonarrSeries(BaseModel):
    """A series as Sonarr knows it. ``tags`` are tag ids (resolve via SonarrTag).

    Keyed primarily on ``tvdb_id`` (Sonarr-native), with ``tmdb_id``/``imdb_id`` also
    present.
    """

    id: int | None = None  # None for lookup results not yet in the library
    tvdb_id: int | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    title: str
    year: int | None = None
    quality_profile_id: int | None = None
    language_profile_id: int | None = None
    series_type: str = "standard"
    season_folder: bool = True
    monitored: bool = False
    seasons: list[SonarrSeason] = Field(default_factory=list)
    tags: list[int] = Field(default_factory=list)

    @classmethod
    def from_sonarr(cls, data: dict[str, Any]) -> SonarrSeries:
        return cls(
            id=data.get("id") or None,  # Sonarr returns id 0 for not-yet-added lookups
            tvdb_id=data.get("tvdbId") or None,
            tmdb_id=data.get("tmdbId") or None,
            imdb_id=data.get("imdbId") or None,
            title=data.get("title") or "?",
            year=data.get("year") or None,
            quality_profile_id=data.get("qualityProfileId"),
            language_profile_id=data.get("languageProfileId"),
            series_type=data.get("seriesType") or "standard",
            season_folder=bool(data.get("seasonFolder", True)),
            monitored=bool(data.get("monitored")),
            seasons=[SonarrSeason.from_sonarr(s) for s in data.get("seasons") or []],
            tags=list(data.get("tags") or []),
        )
