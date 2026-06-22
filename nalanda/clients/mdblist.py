"""MDBList client (official API).

Nalanda reads MDBList through the sanctioned API at ``https://api.mdblist.com``,
which needs an API key (``?apikey=``) and serves public + private lists uniformly.

Three sources, all sharing the same paginated `{movies, shows, pagination}` response:

* **Lists** (`/lists/{user}/{listname}/items`) and **external lists**
  (`/external/lists/{id}/items`, for URLs like ``.../lists/{user}/external/{id}``).
* **Catalog** (`/catalog/movie`) -- MDBList's discover, filterable/sortable by
  cross-source ratings (Rotten Tomatoes, Metacritic, IMDb, MDBList aggregate score).
* **Official** playlists (`/lists/official/{slug}/items`).

Items carry ``ids`` (tmdb/imdb), ``release_date``, and -- when ``append_to_response``
is requested -- ``genres`` (slugs), ``ratings``, ``description``. Pagination is
cursor-based (``pagination.next_cursor`` + ``has_more``); the deprecated ``offset`` is
not used. Catalog items use a slightly different shape (``ids.tmdbid``/``year``),
handled by :meth:`MediaItem.from_mdblist`.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any

from pydantic import TypeAdapter

from ..cache import Cache
from ..http import BaseClient, HTTPError, RateLimiter
from ..logging import get_logger
from ..models import MediaItem, MediaType

log = get_logger(__name__)

_LIST_ADAPTER = TypeAdapter(list[MediaItem])

GenreResolver = Callable[[str], int | None]

MDBLIST_RATE = 1.0  # requests/second for a normal key
MDBLIST_SUPPORTER_RATE = 5.0  # requests/second for a supporter key


class MDBListLimitReached(Exception):
    """Raised when MDBList reports its API/rate quota is exhausted."""


# Items endpoints return up to 1000 per page (cursor-paginated).
_PAGE_SIZE = 1000
# Always enrich items so genre-`except`/`match`, overview and ratings work downstream.
_APPEND = "genres,ratings,poster,description"

_LISTS_RE = re.compile(r"mdblist\.com/lists/(?P<rest>[^?#]+)", re.I)


def parse_list_url(ref: str) -> tuple[str, ...]:
    """Parse an MDBList list reference into a tagged tuple.

    Returns ``("external", id)`` for an external list (``.../external/<digits>``) or
    ``("user", user, listname)`` for a normal list (URL or ``user/listname``).
    """
    match = _LISTS_RE.search(ref)
    rest = (match.group("rest") if match else ref).strip().strip("/")
    external = re.search(r"(?:^|/)external/(\d+)$", rest, re.I)
    if external:
        return ("external", external.group(1))
    parts = rest.split("/")
    if len(parts) == 2 and all(parts):
        return ("user", parts[0], parts[1])
    raise ValueError(f"Cannot parse MDBList list reference: {ref!r}")


def parse_sort(sort_by: str | None) -> tuple[str | None, str]:
    """Split a ``"<field>.<asc|desc>"`` sort into ``(field, order)``
    (order default desc)."""
    if not sort_by:
        return None, "desc"
    field, _, order = sort_by.rpartition(".")
    if order in ("asc", "desc"):
        return field, order
    return sort_by, "desc"


class MDBListClient(BaseClient):
    BASE_URL = "https://api.mdblist.com"

    def __init__(
        self, api_key: str, *, cache: Cache | None = None, **kwargs: Any
    ) -> None:
        if not api_key:
            raise ValueError(
                "MDBList API key is required (mdblist_list uses the official API)"
            )
        self.api_key = api_key
        self._cache = cache
        self._supporter: bool | None = None
        self._supporter_checked = False
        super().__init__(
            self.BASE_URL,
            params={"apikey": api_key},
            limiter=RateLimiter(MDBLIST_RATE),
            **kwargs,
        )

    def _ensure_supporter_checked(self) -> None:
        """Probe ``/user`` once to learn the key's supporter tier and pace accordingly.

        A supporter key is paced at :data:`MDBLIST_SUPPORTER_RATE`, others at
        :data:`MDBLIST_RATE`. A failed or malformed probe is logged and treated as a
        non-supporter; it never aborts the run.
        """
        if self._supporter_checked:
            return
        self._supporter_checked = True
        try:
            data = self.get("user")
        except (HTTPError, ValueError) as exc:
            # ValueError covers a malformed-JSON 200 body (JSONDecodeError); the spec
            # says a failed/malformed /user probe is absorbed, never aborting the run.
            log.debug("MDBList /user probe failed: %s; assuming non-supporter", exc)
            return
        if not isinstance(data, dict):
            return
        self._supporter = bool(data.get("is_supporter"))
        log.info(
            "MDBList key -- supporter: %s; daily API requests: %s, used today: %s",
            self._supporter,
            data.get("api_requests"),
            data.get("api_requests_count"),
        )
        if self._supporter and self._limiter is not None:
            self._limiter.set_rate(MDBLIST_SUPPORTER_RATE)

    def _cached_list(self, key: str, loader):
        """Cache a list/catalog result (a MediaItem list) under ``mdblist.list``."""
        if self._cache is None:
            return loader()
        return self._cache.fetch(
            "mdblist.list",
            key,
            ttl=self._cache._ttl("mdblist.list"),
            loader=loader,
            dump=lambda xs: _LIST_ADAPTER.dump_json(xs).decode(),
            load=lambda s: _LIST_ADAPTER.validate_json(s),
        )

    # --- lists -------------------------------------------------------------

    def get_list(
        self,
        ref: str,
        *,
        sort_by: str | None = None,
        limit: int | None = None,
        genre_resolver: GenreResolver | None = None,
        media: str = "movie",
    ) -> list[MediaItem]:
        """A list's items (movies or shows, by ``media``), cursor-paginated."""
        self._ensure_supporter_checked()
        parsed = parse_list_url(ref)
        if parsed[0] == "external":
            path = f"external/lists/{parsed[1]}/items"
        else:
            path = f"lists/{parsed[1]}/{parsed[2]}/items"
        return self._cached_list(
            f"list:{ref}:{media}:{sort_by}:{limit}",
            lambda: self._paginate(
                path, self._list_params(sort_by), limit, genre_resolver, media
            ),
        )

    def get_official_movies(
        self,
        slug: str,
        *,
        sort_by: str | None = None,
        limit: int | None = None,
        genre_resolver: GenreResolver | None = None,
        media: str = "movie",
    ) -> list[MediaItem]:
        """An official MDBList playlist's items (by slug), cursor-paginated."""
        self._ensure_supporter_checked()
        return self._cached_list(
            f"official:{slug}:{media}:{sort_by}:{limit}",
            lambda: self._paginate(
                f"lists/official/{slug}/items",
                self._list_params(sort_by),
                limit,
                genre_resolver,
                media,
            ),
        )

    def get_catalog(
        self,
        filters: dict[str, Any],
        *,
        limit: int | None = None,
        genre_resolver: GenreResolver | None = None,
        media: str = "movie",
    ) -> list[MediaItem]:
        """MDBList's discover (``/catalog/movie`` or ``/catalog/show``),
        cursor-paginated."""
        self._ensure_supporter_checked()
        path = f"catalog/{'show' if media == 'tv' else 'movie'}"
        norm = "&".join(f"{k}={filters[k]}" for k in sorted(filters))
        key = hashlib.sha1(f"catalog:{media}|{norm}|limit={limit}".encode()).hexdigest()
        return self._cached_list(
            f"catalog:{key}",
            lambda: self._paginate(path, dict(filters), limit, genre_resolver, media),
        )

    # --- internals ---------------------------------------------------------

    @staticmethod
    def _list_params(sort_by: str | None) -> dict[str, Any]:
        field, order = parse_sort(sort_by)
        if field == "random":
            log.warning(
                "MDBList sort=random is not idempotent; the collection will churn"
                " each run"
            )
        params: dict[str, Any] = {"append_to_response": _APPEND}
        if field:
            params["sort"], params["order"] = field, order
        return params

    def _paginate(
        self,
        path: str,
        params: dict[str, Any],
        limit: int | None,
        genre_resolver: GenreResolver | None,
        media: str = "movie",
    ) -> list[MediaItem]:
        if limit is not None and limit <= 0:
            limit = None  # 0 means unlimited
        media_type: MediaType = "tv" if media == "tv" else "movie"
        items: list[MediaItem] = []
        cursor: str | None = None
        seen_cursors: set[str] = (
            set()
        )  # guard against a server returning a repeating cursor
        while True:
            page = {**params, "limit": _PAGE_SIZE}
            if cursor:
                page["cursor"] = cursor
            data = self.get(path, params=page)
            for item in self._items(data, path, media):
                items.append(
                    MediaItem.from_mdblist(
                        item, genre_resolver=genre_resolver, media_type=media_type
                    )
                )
                if limit is not None and len(items) >= limit:
                    return items[:limit]
            pagination = data.get("pagination") if isinstance(data, dict) else None
            cursor = (pagination or {}).get("next_cursor")
            if (
                not cursor
                or (pagination or {}).get("has_more") is False
                or cursor in seen_cursors
            ):
                return items
            seen_cursors.add(cursor)

    @staticmethod
    def _items(data: Any, label: str, media: str = "movie") -> list[dict[str, Any]]:
        """The items array for ``media`` from a response (tolerant of the flat-array
        form).

        Item endpoints return ``{movies, shows, pagination}`` in one payload; a tv
        collection reads ``shows``, a movie collection ``movies``.
        """
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            error = data.get("error")
            if error in ("API Limit Reached!", "API Rate Limit Reached!"):
                raise MDBListLimitReached(f"MDBList API limit reached: {error}")
            if error:
                raise ValueError(f"MDBList '{label}': {error}")
            return data.get("shows" if media == "tv" else "movies") or []
        return []
