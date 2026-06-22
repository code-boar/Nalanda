"""Builders: turn a collection's config into an ordered movie list + source metadata.

A builder key (``tmdb_collection``, ``mdblist_list``, ...) names a source. A collection
may combine several; their results are concatenated and **deduped by provider id**,
preserving first-seen order. The first ``tmdb_collection`` also supplies fallback
collection metadata (overview), which config ``overview`` (literal text) or
``tmdb_overview`` (fetched at runtime) can override.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .clients.mdblist import MDBListClient
from .clients.tmdb import TMDBClient
from .clients.tvdb import TVDBClient
from .config import (
    MOVIE_ONLY_BUILDERS,
    TV_ONLY_BUILDERS,
    BuilderBlock,
    CollectionDef,
    MdblistCatalog,
    MdblistFilter,
    MdblistOfficial,
    SelectFilter,
)
from .logging import get_logger
from .models import MediaItem

log = get_logger(__name__)

# Broad query builders (genre/keyword/company/people/discover) cap at this many movies,
# most-popular first, unless a collection sets `limit:` (and `limit: 0` means
# unlimited).
DEFAULT_LIMIT = 100


@dataclass
class BuilderResult:
    movies: list[MediaItem]
    overview: str | None = None
    # Auto-sourced image URLs (Primary/Thumb/Backdrop), populated for a sole single
    # tmdb_collection; config keys override these per slot downstream.
    primary_url: str | None = None
    thumb_url: str | None = None
    backdrop_url: str | None = None
    # The single source TMDB collection id, iff the collection is exactly one
    # tmdb_collection (no merge, no list) -> eligible for Jellyfin-owned metadata.
    tmdb_collection_id: int | None = None
    # Whether the built pool is release-sorted (vs a sole curated list's server order).
    # Drives the default display order when `order:` is unset.
    release_sorted: bool = True


def tmdb_image_url(path: str | None) -> str | None:
    return f"https://image.tmdb.org/t/p/original{path}" if path else None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _sorted_by_release(movies: list[MediaItem]) -> list[MediaItem]:
    """Chronological by release date; undated titles sort last."""
    return sorted(movies, key=lambda m: m.release_date or "9999-99-99")


def dedupe_movies(movies: list[MediaItem]) -> list[MediaItem]:
    """Dedupe by ANY shared provider id (tmdb / tvdb / imdb), preserving first-seen
    order.

    Checking all three (not just the first present) lets the same title dedupe across
    sources that supply different id spaces -- e.g. a TMDB-sourced show (tmdb id) and
    the same show from TVDB (tvdb id), once enriched. Items with no id are always kept.

    Ids are scoped by ``media_type``: TMDB (and TVDB) movie and TV id spaces are
    distinct, so movie 550 and show 550 are different titles and must not collide in a
    mixed set.
    """
    seen_tmdb: set[tuple[str, int]] = set()
    seen_tvdb: set[tuple[str, int]] = set()
    seen_imdb: set[tuple[str, str]] = set()
    out: list[MediaItem] = []
    for movie in movies:
        mt = movie.media_type
        if (
            (movie.tmdb_id is not None and (mt, movie.tmdb_id) in seen_tmdb)
            or (movie.tvdb_id is not None and (mt, movie.tvdb_id) in seen_tvdb)
            or (movie.imdb_id and (mt, movie.imdb_id) in seen_imdb)
        ):
            continue
        if movie.tmdb_id is not None:
            seen_tmdb.add((mt, movie.tmdb_id))
        if movie.tvdb_id is not None:
            seen_tvdb.add((mt, movie.tvdb_id))
        if movie.imdb_id:
            seen_imdb.add((mt, movie.imdb_id))
        out.append(movie)
    return out


def _named_id(
    collection_name: str,
    value: int | str,
    kind: str,
    resolver: Callable[[str], int | None],
) -> int:
    """An id-or-name value -> a TMDB id. Ints / numeric strings are ids; any other
    string is a NAME resolved via ``resolver``. ``kind`` names the entity in the error
    message."""
    if isinstance(value, int):
        return value
    if value.strip().isdigit():
        return int(value)
    resolved = resolver(value)
    if resolved is None:
        raise ValueError(f"{collection_name!r}: TMDB {kind} not found: {value!r}")
    return resolved


def _numeric_id(collection_name: str, value: int | str, kind: str) -> int:
    """An id-only value -> int. Names aren't resolvable for this entity (e.g. TV
    networks)."""
    if isinstance(value, int):
        return value
    if str(value).strip().isdigit():
        return int(str(value).strip())
    raise ValueError(
        f"{collection_name!r}: TMDB {kind} must be given by id "
        f"(no name search), got {value!r}"
    )


def _keyword_id(collection_name: str, keyword: int | str, tmdb: TMDBClient) -> int:
    """A keyword value -> TMDB keyword id (id / numeric string / name)."""
    return _named_id(
        collection_name, keyword, "keyword", lambda v: tmdb.search_keyword(v)
    )


def _genre_id(
    collection_name: str, genre: int | str, resolve: Callable[[str], int | None]
) -> int:
    """A genre value -> TMDB genre id. Ints / numeric strings are ids; any other
    string is a genre NAME resolved via ``resolve`` (movie or tv genre list)."""
    if isinstance(genre, int):
        return genre
    if genre.strip().isdigit():
        return int(genre)
    resolved = resolve(genre)
    if resolved is None:
        raise ValueError(f"{collection_name!r}: TMDB genre not found: {genre!r}")
    return resolved


def _select_ids(
    value: int | str | list[int | str] | SelectFilter,
    resolve_id: Callable[[int | str], int],
    *,
    bare_op: str,
) -> tuple[str, list[int]] | None:
    """The INCLUDE part of a value as ``(operator, ids)`` for a TMDB ``with_*`` filter.

    ``all`` -> ``","`` (AND), ``any`` -> ``"|"`` (OR); a scalar/bare list uses
    ``bare_op`` (genre defaults to AND, keyword to OR). Returns ``None`` for an
    ``except``-only value.
    """
    if isinstance(value, SelectFilter):
        if value.all is not None:
            values, operator = value.all, ","
        elif value.any is not None:
            values, operator = value.any, "|"
        else:  # except-only -> no include source
            return None
    else:
        values, operator = _as_list(value), bare_op
    return operator, [resolve_id(v) for v in values]


def _select_excluded_ids(
    value: int | str | list[int | str] | SelectFilter,
    resolve_id: Callable[[int | str], int],
) -> list[int]:
    """The EXCLUDE ids of a SelectFilter ``except``; empty for include-only values."""
    if isinstance(value, SelectFilter) and value.excluded is not None:
        return [resolve_id(v) for v in value.excluded]
    return []


def _genre_ids(
    collection_name: str,
    genre: int | str | list[int | str] | SelectFilter,
    resolve: Callable[[str], int | None],
) -> tuple[str, list[int]] | None:
    """The include part of a ``tmdb_genre`` value (bare list = AND)."""
    return _select_ids(
        genre, lambda v: _genre_id(collection_name, v, resolve), bare_op=","
    )


def _genre_excluded_ids(
    collection_name: str,
    genre: int | str | list[int | str] | SelectFilter,
    resolve: Callable[[str], int | None],
) -> list[int]:
    """Genre ids to EXCLUDE (a SelectFilter ``except``)."""
    return _select_excluded_ids(genre, lambda v: _genre_id(collection_name, v, resolve))


def _cap_popular(movies: list[MediaItem], limit: int | None) -> list[MediaItem]:
    """The ``limit`` most-popular movies (no-op when under the limit or unset)."""
    if limit is None or len(movies) <= limit:
        return movies
    return sorted(movies, key=lambda m: m.popularity, reverse=True)[:limit]


# A media-scoped provider id key (mirrors MediaItem.identity_keys):
# (media_type, provider, id).
_IdKey = tuple[str, str, int | str]


def _intersect_movies(sets: list[list[MediaItem]]) -> list[MediaItem]:
    """Items present in EVERY set (by any shared media-scoped provider id), first set's
    order.

    Matching on the same "any shared id" rule as :func:`dedupe_movies` (rather than
    tmdb id alone) keeps a title carrying only a tvdb/imdb id from being dropped from
    an ``all`` set; scoping by media_type stops a movie and a show that share a numeric
    id from intersecting.
    """
    if not sets:
        return []
    key_sets = [{k for m in s for k in m.identity_keys()} for s in sets]
    seen: set[_IdKey] = set()
    out: list[MediaItem] = []
    for movie in sets[0]:
        keys = set(movie.identity_keys())
        if not keys or seen & keys:
            continue  # id-less (unmatchable) or already emitted via a shared id
        if all(not keys.isdisjoint(ks) for ks in key_sets):
            seen |= keys
            out.append(movie)
    return out


def _resolve_select(
    value: int | str | list[int] | list[int | str] | SelectFilter,
    resolve_one: Callable[[int | str], list[MediaItem]],
) -> tuple[list[MediaItem], set[_IdKey], bool]:
    """Resolve a people/company value to ``(included movies, excluded tmdb ids,
    has_include)``.

    ``resolve_one`` maps a single id/name to its films. A scalar/list or ``any`` unions;
    ``all`` intersects (films matching EVERY value); ``except`` subtracts those films
    from the pool.
    """
    if isinstance(value, SelectFilter):
        included: list[MediaItem] = []
        if value.all is not None:
            included = _intersect_movies([resolve_one(v) for v in value.all])
        elif value.any is not None:
            included = [m for v in value.any for m in resolve_one(v)]
        excluded: set[_IdKey] = set()
        if value.excluded is not None:
            for v in value.excluded:
                excluded.update(k for m in resolve_one(v) for k in m.identity_keys())
        return included, excluded, value.all is not None or value.any is not None
    values = _as_list(value)
    return [m for v in values for m in resolve_one(v)], set(), bool(values)


def _mdblist_list_movies(
    value: str | list[str] | MdblistFilter,
    mdblist: Any,
    genre_resolver: Any,
    media: str = "movie",
) -> tuple[list[MediaItem], set[_IdKey]]:
    """Resolve a ``mdblist_list`` value to ``(included items, excluded tmdb ids)``.

    ``url``/scalar/list union the lists; ``all`` intersects; ``except`` subtracts.
    ``sort_by`` and ``limit`` (from a MdblistFilter) are passed to each fetch; ``media``
    picks movies/shows.
    """
    sort_by = value.sort_by if isinstance(value, MdblistFilter) else None
    limit = value.limit if isinstance(value, MdblistFilter) else None

    def fetch(ref: str) -> list[MediaItem]:
        return mdblist.get_list(
            ref,
            sort_by=sort_by,
            limit=limit,
            genre_resolver=genre_resolver,
            media=media,
        )

    excluded: set[_IdKey] = set()
    if isinstance(value, MdblistFilter):
        for ref in value.excluded or []:
            excluded.update(k for m in fetch(ref) for k in m.identity_keys())
        if value.all is not None:
            return _intersect_movies([fetch(ref) for ref in value.all]), excluded
        refs = _as_list(value.url)
    else:
        refs = _as_list(value)
    return [m for ref in refs for m in fetch(ref)], excluded


def _catalog_filters(catalog: MdblistCatalog) -> dict[str, Any]:
    """The non-empty MDBList catalog (``/catalog/movie`` or ``/catalog/show``) query
    params for a MdblistCatalog."""
    filters: dict[str, Any] = {"genre_mode": catalog.genre_mode}
    if catalog.genre:
        filters["genre"] = catalog.genre
    for key in (
        "country",
        "language",
        "score_min",
        "score_max",
        "released_from",
        "released_to",
        "year_min",
        "year_max",
        "runtime_min",
        "runtime_max",
    ):
        if (val := getattr(catalog, key)) is not None:
            filters[key] = val
    if catalog.sort:
        filters["sort"], filters["sort_order"] = catalog.sort, catalog.sort_order
    return filters


# A trailing "(YYYY)" disambiguates a title, e.g. "A Remade Title (1999)".
_TITLE_YEAR_RE = re.compile(r"^(?P<title>.*?)\s*\((?P<year>\d{4})\)\s*$")


def _split_title_year(value: str) -> tuple[str, int | None]:
    """Split a ``"Title (YYYY)"`` value into ``(title, year)``; year is None if
    absent."""
    match = _TITLE_YEAR_RE.match(value)
    if match:
        return match.group("title"), int(match.group("year"))
    return value, None


def _title_movie(
    collection_name: str,
    title: str | int,
    find_by_title: Callable[[str, int | None], MediaItem | None],
) -> MediaItem:
    """Resolve a title (optionally ``"Title (YYYY)"``) to its best-match TMDB
    movie/show."""
    query, year = _split_title_year(str(title))
    movie = find_by_title(query, year)
    if movie is None:
        raise ValueError(f"{collection_name!r}: TMDB title not found: {title!r}")
    return movie


def _sole_collection_id(block: BuilderBlock) -> int | None:
    """The single TMDB collection id iff the block is exactly one tmdb_collection."""
    ids = _as_list(block.tmdb_collection)
    others = (
        _as_list(block.tmdb_movie)
        + _as_list(block.tmdb_title)
        + _as_list(block.tmdb_keyword)
        + _as_list(block.tmdb_genre)
        + _as_list(block.tmdb_actor)
        + _as_list(block.tmdb_director)
        + _as_list(block.tmdb_writer)
        + _as_list(block.tmdb_producer)
        + _as_list(block.tmdb_crew)
        + _as_list(block.tmdb_company)
        + _as_list(block.tmdb_show)
        + _as_list(block.tmdb_network)
        + _as_list(block.tmdb_popular)
        + _as_list(block.tmdb_now_playing)
        + _as_list(block.tmdb_top_rated)
        + _as_list(block.tmdb_upcoming)
        + _as_list(block.tmdb_on_the_air)
        + _as_list(block.tmdb_airing_today)
        + _as_list(block.tmdb_trending_daily)
        + _as_list(block.tmdb_trending_weekly)
        + _as_list(block.tmdb_discover)
        + _as_list(block.tmdb_list)
        + _as_list(block.mdblist_list)
        + _as_list(block.mdblist_catalog)
        + _as_list(block.mdblist_official)
        + _as_list(block.tvdb_show)
        + _as_list(block.tvdb_movie)
        + _as_list(block.tvdb_list)
        + _as_list(block.tvdb_discover)
    )
    return int(ids[0]) if len(ids) == 1 and not others else None


def _resolve_mixed_block(
    name: str,
    block: BuilderBlock,
    *,
    tmdb: TMDBClient | None,
    mdblist: MDBListClient | None,
    tvdb: TVDBClient | None,
    limit: int | None,
) -> tuple[list[MediaItem], str | None, str | None, str | None, str | None, bool]:
    """Resolve a block for a ``media: mixed`` collection.

    Media-specific keys produce their own media; shared keys produce BOTH. Implemented
    by resolving the block once per concrete media -- with the *other* media's keys
    masked off -- and merging the two results. Each pass reuses the single-media logic
    verbatim, so genre id resolution, ``match: all`` and ``except`` all stay correct
    within a media. ``limit`` therefore caps each media independently (up to ``limit``
    movies AND shows).
    """
    movie_block = block.model_copy(update=dict.fromkeys(TV_ONLY_BUILDERS, None))
    tv_block = block.model_copy(update=dict.fromkeys(MOVIE_ONLY_BUILDERS, None))
    movie_items, overview, primary_url, thumb_url, backdrop_url, _ = _resolve_block(
        name,
        movie_block,
        tmdb=tmdb,
        mdblist=mdblist,
        tvdb=tvdb,
        limit=limit,
        media="movie",
    )
    tv_items = _resolve_block(
        name, tv_block, tmdb=tmdb, mdblist=mdblist, tvdb=tvdb, limit=limit, media="tv"
    )[0]
    # Movie and show media types are disjoint, so the merge just interleaves them by
    # release date. Block metadata (overview/images) can only come from a movie
    # tmdb_collection, so carry the movie pass's (run_builders uses it only for a sole
    # single tmdb_collection).
    merged = _sorted_by_release(dedupe_movies(movie_items + tv_items))
    return merged, overview, primary_url, thumb_url, backdrop_url, True


def _resolve_block(
    name: str,
    block: BuilderBlock,
    *,
    tmdb: TMDBClient | None,
    mdblist: MDBListClient | None,
    tvdb: TVDBClient | None = None,
    limit: int | None = None,
    media: str = "movie",
) -> tuple[list[MediaItem], str | None, str | None, str | None, str | None, bool]:
    """Resolve ONE block's sources to items (movie or tv per ``media``) in natural
    order.

    Returns ``(ordered items, overview, primary_url, thumb_url, backdrop_url,
    release_sorted)`` -- the metadata is the first collection's, used only for a sole
    single tmdb_collection. Natural order: a SOLE single curated list keeps its curated
    order; any other combination is unified release-date order (so everything
    interleaves chronologically).
    """
    if media == "mixed":
        return _resolve_mixed_block(
            name, block, tmdb=tmdb, mdblist=mdblist, tvdb=tvdb, limit=limit
        )
    # Default the cap for broad query builders; `limit: 0` means "unlimited".
    effective_limit = (
        DEFAULT_LIMIT if limit is None else (None if limit == 0 else limit)
    )
    is_tv = media == "tv"

    overview: str | None = None
    primary_url: str | None = None
    thumb_url: str | None = None
    backdrop_url: str | None = None
    # chronological pool (collections/movies/titles/keyword/genre/people/company)
    set_movies: list[MediaItem] = []
    list_movies: list[
        MediaItem
    ] = []  # curated/server-order pool (lists/charts/discover)
    # id keys to subtract (people/company/keyword/list `except`)
    excluded_ids: set[_IdKey] = set()
    genre_excluded: list[int] = []
    key_id_sets: list[set[_IdKey]] = []  # one per INCLUDE key, for `match: all`
    curated_count = 0  # curated sources with an include
    chrono_present = False  # chronological sources with an include

    def track(movies: list[MediaItem]) -> None:
        key_id_sets.append({k for m in movies for k in m.identity_keys()})

    def need_tmdb(what: str) -> TMDBClient:
        if tmdb is None:
            raise ValueError(f"{name!r}: {what} requires TMDB to be configured")
        return tmdb

    def need_tvdb(what: str) -> TVDBClient:
        if tvdb is None:
            raise ValueError(f"{name!r}: {what} requires the TVDB client (unavailable)")
        return tvdb

    # Media-dispatched TMDB resolvers, so the builder body stays media-agnostic. Each
    # calls need_tmdb() to get the (narrowed) client -- it is only reached after a
    # block-level guard.
    def _find_by_title(title: str, year: int | None) -> MediaItem | None:
        t = need_tmdb("tmdb_title")
        return (
            t.find_show_by_title(title, year=year)
            if is_tv
            else t.find_movie_by_title(title, year=year)
        )

    def _genre_resolve(genre_name: str) -> int | None:
        return need_tmdb("tmdb_genre").resolve_genre(genre_name, media=media)

    def _genre_items(expr: str) -> list[MediaItem]:
        t = need_tmdb("tmdb_genre")
        fn = t.get_genre_shows if is_tv else t.get_genre_movies
        return fn(expr, limit=effective_limit)

    def _keyword_items(expr: str, without: str | None = None) -> list[MediaItem]:
        t = need_tmdb("tmdb_keyword")
        fn = t.get_keyword_shows if is_tv else t.get_keyword_movies
        return fn(expr, without_keywords=without, limit=effective_limit)

    def _person_items(pid: int, **role: Any) -> list[MediaItem]:
        t = need_tmdb("TMDB people builders")
        fn = t.get_person_shows if is_tv else t.get_person_movies
        return fn(pid, **role)

    def _company_items(cid: int) -> list[MediaItem]:
        t = need_tmdb("tmdb_company")
        fn = t.get_company_shows if is_tv else t.get_company_movies
        return fn(cid, limit=effective_limit)

    def _discover_items(filters: dict[str, Any], lim: int | None) -> list[MediaItem]:
        t = need_tmdb("tmdb_discover")
        fn = t.discover_shows if is_tv else t.discover_movies
        return fn(filters, limit=lim)

    def _chart_items(chart: str, count: int) -> list[MediaItem]:
        t = need_tmdb("tmdb charts")
        fn = t.get_tv_chart if is_tv else t.get_chart
        return fn(chart, count)

    # --- chronological-pool builders ---
    coll_ids = _as_list(block.tmdb_collection)
    if coll_ids:
        t = need_tmdb("tmdb_collection")
        coll_movies: list[MediaItem] = []
        for index, cid in enumerate(coll_ids):
            resolved = t.get_collection(int(cid))
            coll_movies.extend(resolved.movies)
            if (
                index == 0
            ):  # first collection's metadata (used only for a sole collection)
                overview = resolved.overview
                primary_url = tmdb_image_url(resolved.poster_path)
                thumb_url = tmdb_image_url(resolved.thumb_path)
                backdrop_url = tmdb_image_url(resolved.backdrop_path)
        set_movies.extend(coll_movies)
        track(coll_movies)
        chrono_present = True

    movie_ids = _as_list(block.tmdb_movie)
    if movie_ids:
        t = need_tmdb("tmdb_movie")
        movies = [t.get_movie(int(m)) for m in movie_ids]
        set_movies.extend(movies)
        track(movies)
        chrono_present = True

    show_ids = _as_list(block.tmdb_show)  # tv analogue of tmdb_movie
    if show_ids:
        t = need_tmdb("tmdb_show")
        shows = [t.get_show(int(s)) for s in show_ids]
        set_movies.extend(shows)
        track(shows)
        chrono_present = True

    tvdb_show_refs = _as_list(block.tvdb_show)  # id / slug / name -> series (enriched)
    if tvdb_show_refs:
        tv = need_tvdb("tvdb_show")
        shows = [tv.resolve(s, media="tv") for s in tvdb_show_refs]
        set_movies.extend(shows)
        track(shows)
        chrono_present = True

    tvdb_movie_refs = _as_list(
        block.tvdb_movie
    )  # id / slug / name -> movie (TMDB/IMDb ids)
    if tvdb_movie_refs:
        tv = need_tvdb("tvdb_movie")
        movies = [tv.resolve(m, media="movie") for m in tvdb_movie_refs]
        set_movies.extend(movies)
        track(movies)
        chrono_present = True

    title_refs = _as_list(block.tmdb_title)
    if title_refs:
        need_tmdb("tmdb_title")
        movies = [_title_movie(name, t, _find_by_title) for t in title_refs]
        set_movies.extend(movies)
        track(movies)
        chrono_present = True

    # Keyword (id/name; bare list = OR). all=with_keywords AND, any=OR,
    # except=without_keywords (server-side when there's an include; an except-only
    # keyword subtracts its films by id).
    if block.tmdb_keyword is not None:
        t = need_tmdb("tmdb_keyword")
        kw_include = _select_ids(
            block.tmdb_keyword, lambda v: _keyword_id(name, v, t), bare_op="|"
        )
        kw_excluded = _select_excluded_ids(
            block.tmdb_keyword, lambda v: _keyword_id(name, v, t)
        )
        if kw_include is not None:
            operator, ids = kw_include
            without = ",".join(str(i) for i in kw_excluded) if kw_excluded else None
            movies = _keyword_items(operator.join(str(i) for i in ids), without)
            set_movies.extend(movies)
            track(movies)
            chrono_present = True
        else:  # except-only -> subtract each excepted keyword's films
            for kid in kw_excluded:
                hits = _keyword_items(str(kid))
                excluded_ids.update(k for m in hits for k in m.identity_keys())

    # People builders. actor = acting credits; director/writer/producer = crew by
    # department; crew = all crew. A list/`any` unions, `all` intersects, `except`
    # subtracts.
    people_specs = [
        (block.tmdb_actor, {"cast": True}),
        (block.tmdb_director, {"department": "Directing"}),
        (block.tmdb_writer, {"department": "Writing"}),
        (block.tmdb_producer, {"department": "Production"}),
        (block.tmdb_crew, {}),
    ]
    for value, role in people_specs:
        if value is None:
            continue
        t = need_tmdb("TMDB people builders")

        def person_films(person: int | str, role: dict = role) -> list[MediaItem]:
            pid = _named_id(name, person, "person", lambda v: t.search_person(v))
            return dedupe_movies(_person_items(pid, **role))

        included, excluded, has_include = _resolve_select(value, person_films)
        capped = _cap_popular(included, effective_limit)
        set_movies.extend(capped)
        excluded_ids.update(excluded)
        if has_include:
            track(capped)
            chrono_present = True

    if block.tmdb_company is not None:
        t = need_tmdb("tmdb_company")

        def company_films(company: int | str) -> list[MediaItem]:
            cid = _named_id(name, company, "company", lambda v: t.search_company(v))
            return _company_items(cid)

        included, excluded, has_include = _resolve_select(
            block.tmdb_company, company_films
        )
        capped = _cap_popular(included, effective_limit)
        set_movies.extend(capped)
        excluded_ids.update(excluded)
        if has_include:
            track(capped)
            chrono_present = True

    if (
        block.tmdb_network is not None
    ):  # tv only -- the network analogue of tmdb_company
        t = need_tmdb("tmdb_network")

        def network_shows(network: int | str) -> list[MediaItem]:
            nid = _numeric_id(name, network, "network")
            return t.get_network_shows(nid, limit=effective_limit)

        included, excluded, has_include = _resolve_select(
            block.tmdb_network, network_shows
        )
        capped = _cap_popular(included, effective_limit)
        set_movies.extend(capped)
        excluded_ids.update(excluded)
        if has_include:
            track(capped)
            chrono_present = True

    genre_include: tuple[str, list[int]] | None = None
    if block.tmdb_genre is not None:
        need_tmdb("tmdb_genre")
        genre_include = _genre_ids(name, block.tmdb_genre, _genre_resolve)
        genre_excluded = _genre_excluded_ids(name, block.tmdb_genre, _genre_resolve)
        if genre_include is not None:  # `except`-only adds no source, only filters
            operator, gids = genre_include
            movies = _genre_items(operator.join(str(g) for g in gids))
            set_movies.extend(movies)
            track(movies)
            chrono_present = True

    # --- curated-pool builders ---
    if block.tmdb_list is not None:  # set algebra over lists; one curated source
        t = need_tmdb("tmdb_list")
        included, excluded, has_include = _resolve_select(
            block.tmdb_list, lambda lid: t.get_list(int(lid), media=media)
        )
        list_movies.extend(included)
        excluded_ids.update(excluded)
        if has_include:
            track(included)
            curated_count += 1

    # MDBList sources (curated pool). genres on items map to TMDB ids (movie or tv)
    # for except/match.
    genre_resolver = (
        (lambda n: tmdb.resolve_genre(n, media=media)) if tmdb is not None else None
    )
    if block.mdblist_list is not None:
        if mdblist is None:
            raise ValueError(f"{name!r}: mdblist_list requires MDBLIST_API_KEY")
        included, excluded = _mdblist_list_movies(
            block.mdblist_list, mdblist, genre_resolver, media
        )
        list_movies.extend(included)
        excluded_ids.update(excluded)
        track(included)
        curated_count += 1

    if block.mdblist_catalog is not None:
        if mdblist is None:
            raise ValueError(f"{name!r}: mdblist_catalog requires MDBLIST_API_KEY")
        catalog = block.mdblist_catalog
        cat_limit = catalog.limit if catalog.limit is not None else effective_limit
        movies = mdblist.get_catalog(
            _catalog_filters(catalog),
            limit=cat_limit,
            genre_resolver=genre_resolver,
            media=media,
        )
        list_movies.extend(movies)
        track(movies)
        curated_count += 1

    if block.mdblist_official is not None:
        if mdblist is None:
            raise ValueError(f"{name!r}: mdblist_official requires MDBLIST_API_KEY")
        official = block.mdblist_official
        is_model = isinstance(official, MdblistOfficial)
        movies = mdblist.get_official_movies(
            official.slug if is_model else official,
            sort_by=official.sort_by if is_model else None,
            limit=official.limit if is_model else None,
            genre_resolver=genre_resolver,
            media=media,
        )
        list_movies.extend(movies)
        track(movies)
        curated_count += 1

    chart_specs = [
        ("popular", block.tmdb_popular),
        ("now_playing", block.tmdb_now_playing),
        ("top_rated", block.tmdb_top_rated),
        ("upcoming", block.tmdb_upcoming),
        ("on_the_air", block.tmdb_on_the_air),
        ("airing_today", block.tmdb_airing_today),
        ("trending_daily", block.tmdb_trending_daily),
        ("trending_weekly", block.tmdb_trending_weekly),
    ]
    for chart, count in chart_specs:
        if count is None:
            continue
        need_tmdb(f"tmdb_{chart}")
        movies = _chart_items(chart, int(count))
        list_movies.extend(movies)
        track(movies)
        curated_count += 1

    if block.tmdb_discover is not None:  # raw escape hatch; `limit`/`page` intercepted
        need_tmdb("tmdb_discover")
        filters = dict(block.tmdb_discover)
        raw = filters.pop("limit", None)
        filters.pop("page", None)
        # discover's own `limit` wins (0 = unlimited); else inherit the collection
        # default
        disc_limit = (
            effective_limit if raw is None else (None if int(raw) == 0 else int(raw))
        )
        log.debug(
            "%r: tmdb_discover filters=%s limit=%s media=%s",
            name,
            filters,
            disc_limit,
            media,
        )
        movies = _discover_items(filters, disc_limit)
        list_movies.extend(movies)
        track(movies)
        curated_count += 1

    if block.tvdb_list is not None:  # curated TVDB list (filtered to this media)
        tv = need_tvdb("tvdb_list")
        items = tv.get_list(block.tvdb_list, media=media)
        list_movies.extend(items)
        track(items)
        curated_count += 1

    if (
        block.tvdb_discover is not None
    ):  # raw TVDB filter escape hatch; `limit`/`page` intercepted
        tv = need_tvdb("tvdb_discover")
        tvdb_filters = dict(block.tvdb_discover)
        raw = tvdb_filters.pop("limit", None)
        tvdb_filters.pop("page", None)
        disc_limit = (
            effective_limit if raw is None else (None if int(raw) == 0 else int(raw))
        )
        items = tv.discover(tvdb_filters, media=media, limit=disc_limit)
        list_movies.extend(items)
        track(items)
        curated_count += 1

    # --- combine + order ---
    # `release_sorted` records whether the pool is release-sorted (True) or kept in a
    # sole curated source's server order (False) -- it drives the default DisplayOrder.
    release_sorted = True
    if block.match == "all" and key_id_sets:
        # every key must contain the item (by any shared id): keep deduped items whose
        # identity keys intersect every per-key set, then sort chronological.
        pool = [
            m
            for m in dedupe_movies(set_movies + list_movies)
            if (keys := set(m.identity_keys()))
            and all(not keys.isdisjoint(ks) for ks in key_id_sets)
        ]
        ordered = _sorted_by_release(pool)
    elif curated_count == 1 and not chrono_present:
        ordered = list_movies  # sole curated source keeps its server order
        release_sorted = False
    else:
        ordered = _sorted_by_release(set_movies + list_movies)
    if genre_excluded:  # drop anything tagged with an excepted genre (attribute filter)
        drop = set(genre_excluded)
        ordered = [movie for movie in ordered if drop.isdisjoint(movie.genre_ids)]
    # drop people/company/keyword/list `except` items (by any shared id)
    if excluded_ids:
        ordered = [
            m for m in ordered if set(m.identity_keys()).isdisjoint(excluded_ids)
        ]
    return ordered, overview, primary_url, thumb_url, backdrop_url, release_sorted


def run_builders(
    name: str,
    collection: CollectionDef,
    *,
    tmdb: TMDBClient | None,
    mdblist: MDBListClient | None,
    tvdb: TVDBClient | None = None,
) -> BuilderResult:
    """Resolve a collection's blocks (top-level first, then ``append``) -> deduped
    movies.

    Each block is resolved in its natural order; blocks are concatenated in the order
    given so the user controls cross-block ordering, then deduped (first occurrence
    wins).
    """
    blocks: list[BuilderBlock] = [collection]
    if collection.append:
        blocks.extend(collection.append)

    all_movies: list[MediaItem] = []
    base_overview: str | None = None
    base_images: tuple[str | None, str | None, str | None] = (None, None, None)
    base_release_sorted = True
    for index, block in enumerate(blocks):
        block_movies, overview, primary_url, thumb_url, backdrop_url, release_sorted = (
            _resolve_block(
                name,
                block,
                tmdb=tmdb,
                mdblist=mdblist,
                tvdb=tvdb,
                limit=collection.limit,
                media=collection.media,
            )
        )
        all_movies.extend(block_movies)
        if index == 0:
            base_overview = overview
            base_images = (primary_url, thumb_url, backdrop_url)
            base_release_sorted = release_sorted
    # With `append`, the natural order is the block sequence -> source, not release.
    if len(blocks) > 1:
        base_release_sorted = False

    # Jellyfin-owned metadata only when the WHOLE collection is one tmdb_collection
    # (no append blocks, no other sources).
    single_id = None if collection.append else _sole_collection_id(collection)
    overview = base_overview if single_id is not None else None
    primary_url, thumb_url, backdrop_url = (
        base_images if single_id is not None else (None, None, None)
    )
    # `tmdb_overview` pulls a named collection's overview at runtime (e.g. for a merge,
    # which has no sole source to auto-fill from). A literal `overview` still wins,
    # later.
    if collection.tmdb_overview is not None:
        if tmdb is None:
            raise ValueError(
                f"{name!r}: tmdb_overview needs TMDB configured (set TMDB_API_KEY)"
            )
        overview = tmdb.get_collection(collection.tmdb_overview).overview
    return BuilderResult(
        movies=dedupe_movies(all_movies),
        overview=overview,
        primary_url=primary_url,
        thumb_url=thumb_url,
        backdrop_url=backdrop_url,
        tmdb_collection_id=single_id,
        release_sorted=base_release_sorted,
    )
