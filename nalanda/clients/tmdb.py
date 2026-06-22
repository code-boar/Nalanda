"""The Movie Database (TMDB) client.

TMDB spans two API versions that are meant to be used *together*:

* **v3** (``/3/...``) holds the data endpoints we depend on -- movie & collection
  details, find-by-external-id, and search. There is no v4 equivalent for these.
* **v4** (``/4/...``) adds an improved Lists API (paginated + sortable) plus
  account and auth flows.

Both are authenticated with the same **v4 "API Read Access Token"** (a Bearer
JWT), so this client takes that token and routes each call to the right version.
A legacy v3 API key is also accepted and sent as an ``api_key`` query param.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import TypeAdapter

from ..art_select import select_slot
from ..cache import Cache
from ..http import BaseClient, RateLimiter
from ..language import image_language_param, to_subtag
from ..logging import get_logger
from ..models import ArtCandidate, MediaItem, MovieCollection

log = get_logger(__name__)

# One adapter for serializing/deserializing a query result (an ordered MediaItem list).
_LIST_ADAPTER = TypeAdapter(list[MediaItem])

TMDB_RATE = 40  # requests/second; conservative token-bucket ceiling


def _dump_item(item: MediaItem) -> str:
    return item.model_dump_json()


def _dump_opt_item(item: MediaItem | None) -> str:
    return "null" if item is None else item.model_dump_json()


def _load_opt_item(raw: str) -> MediaItem | None:
    return None if raw == "null" else MediaItem.model_validate_json(raw)


def _dump_collection(coll: MovieCollection) -> str:
    return coll.model_dump_json()


class TMDBClient(BaseClient):
    # Version lives in the path (/3/..., /4/...), so the base URL is version-less.
    BASE_URL = "https://api.themoviedb.org"

    def __init__(
        self,
        token: str,
        *,
        language: str = "en-US",
        region: str | None = None,
        cache: Cache | None = None,
        **kwargs: Any,
    ) -> None:
        if not token:
            raise ValueError("A TMDB API Read Access Token (or v3 API key) is required")
        self._cache = cache
        headers: dict[str, str] = {}
        params: dict[str, Any] = {"language": language}
        if region:
            params["region"] = region  # applied to charts/discover; ignored elsewhere
        # v4 read access tokens are JWTs (three dot-separated parts); a v3 key is a
        # 32-char hex string that must travel in the api_key query param instead.
        if token.count(".") >= 2:
            headers["Authorization"] = f"Bearer {token}"
        else:
            params["api_key"] = token
        self.language = (
            language  # used to pick the preferred-language Thumb (titled backdrop)
        )
        self._genre_caches: dict[
            str, dict[str, int]
        ] = {}  # lazy media -> {name -> id} maps
        super().__init__(
            self.BASE_URL,
            headers=headers,
            params=params,
            limiter=RateLimiter(TMDB_RATE),
            **kwargs,
        )

    # --- cache helpers -----------------------------------------------------

    def _cached(self, namespace, key, loader, *, dump=json.dumps, load=json.loads):
        """Run ``loader`` through the cache (or directly if no cache is configured)."""
        if self._cache is None:
            return loader()
        return self._cache.fetch(
            namespace,
            key,
            ttl=self._cache._ttl(namespace),
            loader=loader,
            dump=dump,
            load=load,
        )

    def _query_key(
        self, path: str, params: dict[str, Any], *, media: str, limit
    ) -> str:
        """A stable fingerprint of a paginated query: endpoint + sorted params (minus
        the pagination cursor) + media + language + region + the requested limit."""
        norm = "&".join(f"{k}={params[k]}" for k in sorted(params) if k != "page")
        region = self._default_params.get("region", "")
        raw = f"{path}|{media}|{self.language}|{region}|{norm}|limit={limit}"
        return hashlib.sha1(raw.encode()).hexdigest()

    def _cached_query(self, namespace, path, params, *, media, limit, loader):
        """Cache a query result (a MediaItem list) under ``namespace``, keyed by the
        query."""
        if self._cache is None:
            return loader()
        return self._cache.fetch(
            namespace,
            self._query_key(path, params, media=media, limit=limit),
            ttl=self._cache._ttl(namespace),
            loader=loader,
            dump=lambda xs: _LIST_ADAPTER.dump_json(xs).decode(),
            load=lambda s: _LIST_ADAPTER.validate_json(s),
        )

    def _cached_paged(self, namespace, path, params, *, media, limit):
        """Cache a paginated (``_paged_results``) query. The loader keeps its ``limit``
        -- the key includes ``limit`` so different caps don't collide and a bounded
        query never over-fetches to fill the cache."""
        return self._cached_query(
            namespace,
            path,
            params,
            media=media,
            limit=limit,
            loader=lambda: self._paged_results(path, params, limit=limit, media=media),
        )

    # --- v3 data endpoints -------------------------------------------------

    def get_movie(self, movie_id: int) -> MediaItem:
        """Movie details, with IMDb id appended."""
        return self._cached(
            "tmdb.record",
            f"movie:{movie_id}:{self.language}",
            lambda: MediaItem.from_tmdb(
                self.get(
                    f"3/movie/{movie_id}",
                    params={"append_to_response": "external_ids"},
                )
            ),
            dump=_dump_item,
            load=MediaItem.model_validate_json,
        )

    def get_collection(self, collection_id: int) -> MovieCollection:
        """A TMDB collection and its member movies (``parts``), in release-date order.

        TMDB returns ``parts`` in an undefined internal order (not release/popularity/
        id); the website shows them by release date and so do we. ``images`` is appended
        so we can pick the Backdrop (textless) and Thumb (titled) the way Jellyfin does.
        """
        return self._cached(
            "tmdb.record",
            f"collection:{collection_id}:{self.language}",
            lambda: self._fetch_collection(collection_id),
            dump=_dump_collection,
            load=MovieCollection.model_validate_json,
        )

    def _fetch_collection(self, collection_id: int) -> MovieCollection:
        # TMDB filters appended images by the request `language`; image languages are
        # 2-letter (`en`), so `language=en-GB` matches none and drops *every* backdrop
        # -- silently losing the titled one (Thumb) entirely. Ask for the languages we
        # actually select among: preferred (2-letter) + English + textless (`null`).
        pref = to_subtag(self.language)
        data = self.get(
            f"3/collection/{collection_id}",
            params={
                "append_to_response": "images",
                "include_image_language": image_language_param(pref),
            },
        )
        parts = sorted(
            data.get("parts", []), key=lambda p: p.get("release_date") or "9999-99-99"
        )
        movies = [MediaItem.from_tmdb(part) for part in parts]
        # Build provider-agnostic candidates (in response order, so ties resolve as
        # before) and let the shared selector pick per slot. Primary is the collection's
        # top-level poster -- a single candidate, NOT a pick from the posters[] array;
        # each backdrop is a Thumb (titled) or Backdrop (textless) candidate the way
        # Jellyfin types them.
        candidates: list[ArtCandidate] = []
        if data.get("poster_path"):
            candidates.append(ArtCandidate(slot="Primary", path=data["poster_path"]))
        for b in (data.get("images") or {}).get("backdrops") or []:
            code = (b.get("iso_639_1") or "").casefold()
            has_text = bool(code) and code != "xx"
            candidates.append(
                ArtCandidate(
                    slot="Thumb" if has_text else "Backdrop",
                    path=b.get("file_path") or "",
                    lang=code or None,
                    has_text=has_text,
                    score=(
                        round(b.get("vote_average") or 0, 1),
                        b.get("vote_count") or 0,
                    ),
                )
            )
        primary = select_slot(candidates, "Primary", preferred_lang=pref)
        thumb = select_slot(candidates, "Thumb", preferred_lang=pref)
        backdrop = select_slot(candidates, "Backdrop", preferred_lang=pref)
        return MovieCollection(
            tmdb_id=data["id"],
            name=data.get("name") or "?",
            overview=data.get("overview") or None,
            poster_path=(primary.path if primary else None)
            or (data.get("poster_path") or None),
            # Jellyfin types a TMDB backdrop WITH a language as Thumb, WITHOUT as
            # Backdrop.
            backdrop_path=(backdrop.path if backdrop else None)
            or (data.get("backdrop_path") or None),
            thumb_path=(thumb.path or None) if thumb else None,
            movies=movies,
        )

    def find_by_imdb(self, imdb_id: str) -> MediaItem | None:
        """Resolve an IMDb id (ttXXXXXXX) to a TMDB movie, or ``None``."""
        data = self.get(f"3/find/{imdb_id}", params={"external_source": "imdb_id"})
        results = data.get("movie_results") or []
        return MediaItem.from_tmdb(results[0]) if results else None

    def search_movie(self, query: str, *, year: int | None = None) -> list[MediaItem]:
        params: dict[str, Any] = {"query": query}
        if year is not None:
            params["year"] = year
        data = self.get("3/search/movie", params=params)
        return [MediaItem.from_tmdb(m) for m in data.get("results", [])]

    def find_movie_by_title(
        self, title: str, *, year: int | None = None
    ) -> MediaItem | None:
        """TMDB's best-match movie for a title, with full details (``None`` if no
        match).

        Search ordering is used as-is -- pass ``year`` (or a ``"Title (YYYY)"`` value
        upstream) to disambiguate when several films share a title. The chosen result
        is re-fetched via :meth:`get_movie` so it carries the same detail (incl. IMDb
        id) as an id-based ``tmdb_movie``.
        """
        return self._cached(
            "tmdb.resolve",
            f"title:movie:{title}:{year}:{self.language}",
            lambda: self._find_movie_by_title(title, year),
            dump=_dump_opt_item,
            load=_load_opt_item,
        )

    def _find_movie_by_title(self, title: str, year: int | None) -> MediaItem | None:
        results = self.search_movie(title, year=year)
        if not results:
            return None
        best = results[0]
        log.debug(
            "tmdb_title %r -> %r (%s) [tmdb %s]",
            title,
            best.title,
            (best.release_date or "?")[:4],
            best.tmdb_id,
        )
        return self.get_movie(best.tmdb_id) if best.tmdb_id is not None else best

    # --- v4 lists ----------------------------------------------------------

    def get_list(self, list_id: int | str, *, media: str = "movie") -> list[MediaItem]:
        """Items of a TMDB **v4** list matching ``media`` (movie | tv),
        following pagination."""
        return self._cached(
            "tmdb.list",
            f"list:{list_id}:{media}:{self.language}",
            lambda: self._get_list(list_id, media),
            dump=lambda xs: _LIST_ADAPTER.dump_json(xs).decode(),
            load=lambda s: _LIST_ADAPTER.validate_json(s),
        )

    def _get_list(self, list_id: int | str, media: str) -> list[MediaItem]:
        parse = MediaItem.from_tmdb_tv if media == "tv" else MediaItem.from_tmdb
        items: list[MediaItem] = []
        page = 1
        while True:
            data = self.get(f"4/list/{list_id}", params={"page": page})
            for item in data.get("results", []):
                if item.get("media_type", "movie") == media:
                    items.append(parse(item))
            if page >= int(data.get("total_pages") or 1):
                break
            page += 1
        return items

    # --- v3 paginated lists / discover -------------------------------------

    # TMDB paginated endpoints serve at most 500 pages (10,000 results).
    _PAGE_CAP = 500

    def _paged_results(
        self,
        path: str,
        params: dict[str, Any],
        *,
        limit: int | None = None,
        media: str = "movie",
    ) -> list[MediaItem]:
        """Items from any ``{results, page, total_pages}`` endpoint, following pages.

        ``media`` ("movie" | "tv") picks the parser (movie vs TV shape). Stops at
        ``limit`` results (if given) or TMDB's hard 500-page cap, whichever comes first;
        ``limit`` truncation is intentional, only the page cap is logged.
        """
        parse = MediaItem.from_tmdb_tv if media == "tv" else MediaItem.from_tmdb
        items: list[MediaItem] = []
        page = 1
        while True:
            data = self.get(path, params={**params, "page": page})
            for item in data.get("results", []):
                items.append(parse(item))
            if limit is not None and len(items) >= limit:
                return items[:limit]
            total_pages = int(data.get("total_pages") or 1)
            if page >= min(total_pages, self._PAGE_CAP):
                if total_pages > self._PAGE_CAP:
                    log.warning(
                        "%s %s spans %d pages; capped at TMDB's %d-page limit"
                        " (%d results)",
                        path,
                        params,
                        total_pages,
                        self._PAGE_CAP,
                        self._PAGE_CAP * 20,
                    )
                break
            page += 1
        return items

    def _search_id(self, path: str, name: str) -> int | None:
        """Resolve a name to a TMDB id via a search endpoint (exact case-fold match
        preferred, else the first / most-popular result)."""
        data = self.get(path, params={"query": name})
        results = data.get("results") or []
        for result in results:
            if (result.get("name") or "").casefold() == name.casefold():
                return result.get("id")
        return results[0].get("id") if results else None

    def search_keyword(self, name: str) -> int | None:
        """Resolve a keyword name to its TMDB id."""
        return self._cached(
            "tmdb.resolve",
            f"keyword:{name}",
            lambda: self._search_id("3/search/keyword", name),
        )

    def get_keyword_movies(
        self,
        with_keywords: str,
        *,
        without_keywords: str | None = None,
        limit: int | None = None,
    ) -> list[MediaItem]:
        """Movies matching a ``with_keywords`` expression, via Discover
        (most-popular first).

        ``with_keywords`` joins ids by ``,`` (AND) or ``|`` (OR); ``without_keywords``
        excludes ids server-side.
        """
        params: dict[str, Any] = {"with_keywords": with_keywords}
        if without_keywords:
            params["without_keywords"] = without_keywords
        return self._cached_paged(
            "tmdb.query", "3/discover/movie", params, media="movie", limit=limit
        )

    @staticmethod
    def _normalize_genre(name: str) -> str:
        """Lower-case, alphanumerics only -- so 'Science-Fiction' matches
        'Science Fiction'."""
        return "".join(ch for ch in name.casefold() if ch.isalnum())

    def _genres(self, media: str = "movie") -> dict[str, int]:
        """TMDB's genre list for ``media`` as a name -> id map
        (in-memory + disk cached)."""
        if media not in self._genre_caches:
            self._genre_caches[media] = self._cached(
                "tmdb.resolve",
                f"genres:{media}:{self.language}",
                lambda: self._fetch_genre_map(media),
            )
        return self._genre_caches[media]

    def _fetch_genre_map(self, media: str) -> dict[str, int]:
        data = self.get(f"3/genre/{'tv' if media == 'tv' else 'movie'}/list")
        mapping: dict[str, int] = {}
        for genre in data.get("genres", []):
            gid, gname = genre.get("id"), genre.get("name") or ""
            if gid is None:
                continue
            mapping[gname.casefold()] = gid
            mapping[self._normalize_genre(gname)] = gid
        return mapping

    def resolve_genre(self, name: str, *, media: str = "movie") -> int | None:
        """Resolve a genre name to its TMDB id (case/punctuation-insensitive);
        movie or tv."""
        genres = self._genres(media)
        key = name.casefold()
        if key in genres:
            return genres[key]
        return genres.get(self._normalize_genre(name))

    def get_genre_movies(
        self, with_genres: str, *, limit: int | None = None
    ) -> list[MediaItem]:
        """Movies matching a ``with_genres`` expression, via Discover
        (most-popular first).

        ``with_genres`` is a pre-assembled TMDB filter -- ids joined by ``,`` (AND)
        or ``|`` (OR), e.g. ``"28,12"`` or ``"27|35"``.
        """
        return self._cached_paged(
            "tmdb.query",
            "3/discover/movie",
            {"with_genres": with_genres},
            media="movie",
            limit=limit,
        )

    # --- people -----------------------------------------------------------

    def search_person(self, name: str) -> int | None:
        """Resolve a person's name to their TMDB id (most-popular match wins)."""
        return self._cached(
            "tmdb.resolve",
            f"person:{name}",
            lambda: self._search_id("3/search/person", name),
        )

    def get_person_movies(
        self, person_id: int, *, cast: bool = False, department: str | None = None
    ) -> list[MediaItem]:
        """A person's movie credits. ``cast=True`` -> acting roles; otherwise crew,
        optionally filtered to one ``department`` (e.g. ``"Directing"``);
        ``None`` = all crew.
        """

        def load() -> list[MediaItem]:
            data = self.get(f"3/person/{person_id}/movie_credits")
            if cast:
                items = data.get("cast") or []
            else:
                crew = data.get("crew") or []
                items = [
                    c
                    for c in crew
                    if department is None or c.get("department") == department
                ]
            return [MediaItem.from_tmdb(item) for item in items]

        return self._cached_query(
            "tmdb.query",
            f"3/person/{person_id}/movie_credits",
            {"cast": str(cast), "department": department or ""},
            media="movie",
            limit=None,
            loader=load,
        )

    # --- company ----------------------------------------------------------

    def search_company(self, name: str) -> int | None:
        """Resolve a production company name to its TMDB id.

        Company search is NOT prominence-ordered and names can collide (two companies
        may share a name), so we pick the candidate with the most movies -- preferring
        exact name matches, and capping how many candidates we probe.
        """
        return self._cached(
            "tmdb.resolve", f"company:{name}", lambda: self._search_company(name)
        )

    def _search_company(self, name: str) -> int | None:
        results = (
            self.get("3/search/company", params={"query": name}).get("results") or []
        )
        if not results:
            return None
        exact = [
            r for r in results if (r.get("name") or "").casefold() == name.casefold()
        ]
        candidates = (exact or results)[:5]
        if len(candidates) == 1:
            return candidates[0].get("id")

        def movie_count(company_id: int) -> int:
            data = self.get(
                "3/discover/movie", params={"with_companies": company_id, "page": 1}
            )
            return int(data.get("total_results") or 0)

        return max(candidates, key=lambda r: movie_count(r.get("id"))).get("id")

    def get_company_movies(
        self, company_id: int, *, limit: int | None = None
    ) -> list[MediaItem]:
        """Movies from a production company, via Discover (most-popular first)."""
        return self._cached_paged(
            "tmdb.query",
            "3/discover/movie",
            {"with_companies": company_id},
            media="movie",
            limit=limit,
        )

    # --- raw discover (escape hatch) --------------------------------------

    def discover_movies(
        self, filters: dict[str, Any], *, limit: int | None = None
    ) -> list[MediaItem]:
        """Run a raw TMDB Discover query from a filter dict, honouring an optional
        limit."""
        return self._cached_paged(
            "tmdb.query", "3/discover/movie", filters, media="movie", limit=limit
        )

    # --- charts -----------------------------------------------------------

    _CHART_PATHS = {
        "popular": "3/movie/popular",
        "now_playing": "3/movie/now_playing",
        "top_rated": "3/movie/top_rated",
        "upcoming": "3/movie/upcoming",
        "trending_daily": "3/trending/movie/day",
        "trending_weekly": "3/trending/movie/week",
    }

    def get_chart(self, chart: str, limit: int) -> list[MediaItem]:
        """The first ``limit`` movies from a TMDB chart, in the chart's own order."""
        return self._cached_paged(
            "tmdb.chart", self._CHART_PATHS[chart], {}, media="movie", limit=limit
        )

    # --- TV (shows) -------------------------------------------------------
    # The show analogues of the movie endpoints above: `/3/tv/...`, `/3/discover/tv`,
    # `/3/search/tv`, `/3/person/{id}/tv_credits`, and the TV charts. All parse via
    # MediaItem.from_tmdb_tv (name/first_air_date), and pass media="tv" to
    # _paged_results.

    def get_show(self, show_id: int) -> MediaItem:
        """Show details, with TVDB + IMDb ids appended (the ids Sonarr/Jellyfin key
        on)."""
        return self._cached(
            "tmdb.record",
            f"show:{show_id}:{self.language}",
            lambda: MediaItem.from_tmdb_tv(
                self.get(
                    f"3/tv/{show_id}", params={"append_to_response": "external_ids"}
                )
            ),
            dump=_dump_item,
            load=MediaItem.model_validate_json,
        )

    def search_tv(self, query: str, *, year: int | None = None) -> list[MediaItem]:
        params: dict[str, Any] = {"query": query}
        if year is not None:
            params["first_air_date_year"] = year
        data = self.get("3/search/tv", params=params)
        return [MediaItem.from_tmdb_tv(m) for m in data.get("results", [])]

    def find_show_by_title(
        self, title: str, *, year: int | None = None
    ) -> MediaItem | None:
        """TMDB's best-match show for a title, re-fetched for full detail
        (TVDB/IMDb ids)."""
        return self._cached(
            "tmdb.resolve",
            f"title:tv:{title}:{year}:{self.language}",
            lambda: self._find_show_by_title(title, year),
            dump=_dump_opt_item,
            load=_load_opt_item,
        )

    def _find_show_by_title(self, title: str, year: int | None) -> MediaItem | None:
        results = self.search_tv(title, year=year)
        if not results:
            return None
        best = results[0]
        log.debug(
            "tmdb_title(tv) %r -> %r (%s) [tmdb %s]",
            title,
            best.title,
            (best.release_date or "?")[:4],
            best.tmdb_id,
        )
        return self.get_show(best.tmdb_id) if best.tmdb_id is not None else best

    def get_keyword_shows(
        self,
        with_keywords: str,
        *,
        without_keywords: str | None = None,
        limit: int | None = None,
    ) -> list[MediaItem]:
        """Shows matching a ``with_keywords`` expression, via Discover
        (most-popular first)."""
        params: dict[str, Any] = {"with_keywords": with_keywords}
        if without_keywords:
            params["without_keywords"] = without_keywords
        return self._cached_paged(
            "tmdb.query", "3/discover/tv", params, media="tv", limit=limit
        )

    def get_genre_shows(
        self, with_genres: str, *, limit: int | None = None
    ) -> list[MediaItem]:
        """Shows matching a ``with_genres`` expression, via Discover
        (most-popular first)."""
        return self._cached_paged(
            "tmdb.query",
            "3/discover/tv",
            {"with_genres": with_genres},
            media="tv",
            limit=limit,
        )

    def get_company_shows(
        self, company_id: int, *, limit: int | None = None
    ) -> list[MediaItem]:
        """Shows from a production company, via Discover (most-popular first)."""
        return self._cached_paged(
            "tmdb.query",
            "3/discover/tv",
            {"with_companies": company_id},
            media="tv",
            limit=limit,
        )

    def get_network_shows(
        self, network_id: int, *, limit: int | None = None
    ) -> list[MediaItem]:
        """Shows from a TV network (the TV analogue of a movie company),
        via Discover."""
        return self._cached_paged(
            "tmdb.query",
            "3/discover/tv",
            {"with_networks": network_id},
            media="tv",
            limit=limit,
        )

    def get_person_shows(
        self, person_id: int, *, cast: bool = False, department: str | None = None
    ) -> list[MediaItem]:
        """A person's TV credits. ``cast=True`` -> acting roles; otherwise crew
        (optionally filtered to one ``department``; ``None`` = all crew)."""

        def load() -> list[MediaItem]:
            data = self.get(f"3/person/{person_id}/tv_credits")
            if cast:
                items = data.get("cast") or []
            else:
                crew = data.get("crew") or []
                items = [
                    c
                    for c in crew
                    if department is None or c.get("department") == department
                ]
            return [MediaItem.from_tmdb_tv(item) for item in items]

        return self._cached_query(
            "tmdb.query",
            f"3/person/{person_id}/tv_credits",
            {"cast": str(cast), "department": department or ""},
            media="tv",
            limit=None,
            loader=load,
        )

    def discover_shows(
        self, filters: dict[str, Any], *, limit: int | None = None
    ) -> list[MediaItem]:
        """Run a raw TMDB TV Discover query from a filter dict, honouring an optional
        limit."""
        return self._cached_paged(
            "tmdb.query", "3/discover/tv", filters, media="tv", limit=limit
        )

    _TV_CHART_PATHS = {
        "popular": "3/tv/popular",
        "top_rated": "3/tv/top_rated",
        "on_the_air": "3/tv/on_the_air",
        "airing_today": "3/tv/airing_today",
        "trending_daily": "3/trending/tv/day",
        "trending_weekly": "3/trending/tv/week",
    }

    def get_tv_chart(self, chart: str, limit: int) -> list[MediaItem]:
        """The first ``limit`` shows from a TMDB TV chart, in the chart's own order."""
        return self._cached_paged(
            "tmdb.chart", self._TV_CHART_PATHS[chart], {}, media="tv", limit=limit
        )
