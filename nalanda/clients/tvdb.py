"""TheTVDB v4 client.

A builder source for shows (TVDB ids are Sonarr-native) and movies (TVDB tracks both;
its movie records carry remote TMDB/IMDb ids, so a TVDB movie can feed Radarr/Jellyfin).

Auth is a bearer-token flow: ``POST /login`` with an API key returns a token valid
~1 month, sent thereafter as ``Authorization: Bearer``. TVDB v4 keys are
**per-project** (TVDB's own docs: "individual users shouldn't need an API key"), and
the limits are per-IP (~200 IPs), so Nalanda **ships its own project key** -- no
per-user setup. The key lives obfuscated in ``bundled_tvdb_key``
(XOR-against-a-derived-keystream, base85, interleaved into two strands); that is
obfuscation against secret-scanners and casual viewers, **not** encryption -- to
rotate, re-run ``scripts/encode_tvdb_key.py`` and push the new strands. The token is
fetched lazily on the first call and cached for the client's lifetime.

Every response is wrapped ``{"status": ..., "data": ...}``; list endpoints add
``{"links": {...}}`` for pagination. Records expose remote ids only on the ``/extended``
variant, so movie lookups (which need TMDB/IMDb for Radarr/Jellyfin) fetch extended.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from pydantic import TypeAdapter

from ..cache import Cache
from ..http import BaseClient, HTTPError, RateLimiter
from ..logging import get_logger
from ..models import MediaItem, MediaType

log = get_logger(__name__)

TVDB_RATE = 10  # requests/second; conservative token-bucket ceiling

_LIST_ADAPTER = TypeAdapter(list[MediaItem])


def _dump_item(item: MediaItem) -> str:
    return item.model_dump_json()


# TVDB's filter endpoints require a country + language; default to these when unset.
_DEFAULT_COUNTRY = "usa"
_DEFAULT_LANG = "eng"

# --- bundled API key obfuscation ---------------------------------------------------
# Goal: keep the shipped project key out of plaintext grep and automated
# secret-scanners. This is OBFUSCATION, NOT SECURITY -- the running app must
# reproduce the plaintext key, so a determined reader can recover it. The seed is NOT
# secret; it just ensures a blind base64/85 pass yields noise rather than something
# key-shaped.
_OBF_SEED = b"nalanda/tvdb/v4"


def _keystream(n: int) -> bytes:
    buf = bytearray()
    block = 0
    while len(buf) < n:
        buf += hashlib.sha256(_OBF_SEED + block.to_bytes(2, "big")).digest()
        block += 1
    return bytes(buf[:n])


def _xor(data: bytes) -> bytes:
    """XOR against the derived keystream (its own inverse)."""
    return bytes(b ^ k for b, k in zip(data, _keystream(len(data))))


def _strand(key: str) -> tuple[str, str]:
    """Obfuscate a key into two strands -- base85 of XOR'd bytes, split at stride 2
    (offline)."""
    blob = base64.b85encode(_xor(key.encode())).decode()
    return blob[0::2], blob[1::2]  # even-index chars, odd-index chars


def _unstrand(k1: str, k2: str) -> str:
    """Reassemble the stride-2 interleave and decode -- the inverse of
    :func:`_strand`."""
    chars: list[str] = []
    for i, ch in enumerate(k1):  # k1 holds even positions (>= k2 length)
        chars.append(ch)
        if i < len(k2):
            chars.append(k2[i])
    return _xor(base64.b85decode("".join(chars))).decode()


# The bundled TVDB project key, obfuscated. Regenerate both strands with
# `uv run python scripts/encode_tvdb_key.py <new-key>` when rotating.
_K1 = "=oX1t_9)2!GaACr^bNzMJ0}"
_K2 = "WCQt2WaUU(dt@GYSfs{6S{"


def bundled_tvdb_key() -> str:
    """The shipped TVDB project key (obfuscation only -- see the note above)."""
    return _unstrand(_K1, _K2)


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip()) if value not in (None, "") else None
    except TypeError, ValueError:
        return None


def _remote_ids(record: dict[str, Any], key: str) -> dict[str, Any]:
    """Pull ``tmdb_id`` / ``imdb_id`` out of a TVDB remote-ids array (``key`` names it).

    Each entry is ``{id, type, sourceName}``; we classify by ``sourceName`` (TVDB calls
    TMDB "TheMovieDB"). Only present on the EXTENDED and search shapes.
    """
    out: dict[str, Any] = {}
    for entry in record.get(key) or []:
        source = (entry.get("sourceName") or "").casefold()
        rid = entry.get("id")
        if not rid:
            continue
        if "imdb" in source:
            out["imdb_id"] = str(rid)
        elif "themoviedb" in source or source == "tmdb":
            out["tmdb_id"] = _int(rid)
    return out


class TVDBClient(BaseClient):
    BASE_URL = "https://api4.thetvdb.com/v4"

    def __init__(
        self,
        api_key: str | None = None,
        pin: str | None = None,
        *,
        cache: Cache | None = None,
        **kwargs: Any,
    ) -> None:
        # Production passes nothing -> the bundled project key is used (resolved lazily
        # at login). `api_key`/`pin` are an injection seam for tests; there is no
        # user-facing override (TVDB v4 keys are per-project, so end users don't supply
        # one).
        self._api_key = api_key or None
        self._pin = pin or None
        self._token: str | None = None
        self._cache = cache
        super().__init__(self.BASE_URL, limiter=RateLimiter(TVDB_RATE), **kwargs)

    def _cached(self, namespace, key, loader, *, dump=json.dumps, load=json.loads):
        """Run ``loader`` through the cache (or directly if none). A hit returns before
        any ``self.get`` call, so it never triggers the lazy login."""
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

    # --- auth -------------------------------------------------------------

    def request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        """Ensure a bearer token before any non-login call (logs in lazily, once)."""
        if path != "login" and self._token is None:
            self._login()
        return super().request_json(method, path, **kwargs)

    def _login(self) -> None:
        body: dict[str, Any] = {"apikey": self._api_key or bundled_tvdb_key()}
        if self._pin:
            body["pin"] = self._pin
        data = self.post("login", json=body)  # path == "login" -> no recursion
        token = ((data or {}).get("data") or {}).get("token")
        if not token:
            raise HTTPError("TVDB login failed: no token in response")
        self._token = token
        self._session.headers["Authorization"] = f"Bearer {token}"
        # Attribution (TVDB API terms). Logged once per run, when TVDB data is
        # actually used.
        log.info(
            "Metadata provided by TheTVDB. Consider subscribing: https://thetvdb.com/subscribe"
        )

    # --- record -> MediaItem ----------------------------------------------

    def _series_item(self, rec: dict[str, Any]) -> MediaItem:
        return MediaItem(
            tvdb_id=_int(rec.get("id")),
            media_type="tv",
            title=rec.get("name") or "?",
            year=_int(rec.get("year")),
            release_date=rec.get("firstAired") or None,
            **_remote_ids(rec, "remoteIds"),
        )

    def _movie_item(self, rec: dict[str, Any]) -> MediaItem:
        return MediaItem(
            tvdb_id=_int(rec.get("id")),
            media_type="movie",
            title=rec.get("name") or "?",
            year=_int(rec.get("year")),
            **_remote_ids(rec, "remoteIds"),
        )

    def _search_item(self, rec: dict[str, Any], media: str) -> MediaItem:
        media_type: MediaType = "tv" if media == "tv" else "movie"
        return MediaItem(
            tvdb_id=_int(rec.get("tvdb_id")),
            media_type=media_type,
            title=rec.get("name") or "?",
            year=_int(rec.get("year")),
            **_remote_ids(rec, "remote_ids"),
        )

    @staticmethod
    def _data(payload: Any) -> Any:
        return (payload or {}).get("data") if isinstance(payload, dict) else None

    # --- single records ----------------------------------------------------

    def get_series(self, tvdb_id: int, *, extended: bool = False) -> MediaItem:
        """A series by TVDB id. ``extended`` also pulls remote (TMDB/IMDb) ids."""
        return self._cached(
            "tvdb.record",
            # extended carries remote ids -> distinct payload
            f"series:{tvdb_id}:{extended}",
            lambda: self._series_item(
                self._data(
                    self.get(f"series/{tvdb_id}{'/extended' if extended else ''}")
                )
                or {}
            ),
            dump=_dump_item,
            load=MediaItem.model_validate_json,
        )

    def get_movie(self, tvdb_id: int, *, extended: bool = False) -> MediaItem:
        """A movie by TVDB id. ``extended`` pulls remote ids (needed for
        Radarr/Jellyfin)."""
        return self._cached(
            "tvdb.record",
            f"movie:{tvdb_id}:{extended}",
            lambda: self._movie_item(
                self._data(
                    self.get(f"movies/{tvdb_id}{'/extended' if extended else ''}")
                )
                or {}
            ),
            dump=_dump_item,
            load=MediaItem.model_validate_json,
        )

    # --- search (name resolution) -----------------------------------------

    def search(
        self,
        query: str,
        *,
        media: str = "tv",
        year: int | None = None,
        limit: int | None = None,
    ) -> list[MediaItem]:
        """Search series/movies by name, newest-relevance order (TVDB's own ranking)."""
        params: dict[str, Any] = {
            "query": query,
            "type": "series" if media == "tv" else "movie",
        }
        if year is not None:
            params["year"] = year
        if limit is not None:
            params["limit"] = limit
        results = self._data(self.get("search", params=params)) or []
        return [self._search_item(r, media) for r in results]

    def resolve(self, value: int | str, *, media: str) -> MediaItem:
        """Resolve a TVDB id (or numeric string) or a NAME to a full (extended) item."""
        getter = self.get_series if media == "tv" else self.get_movie
        if isinstance(value, int) or str(value).strip().isdigit():
            return getter(
                int(str(value).strip()), extended=True
            )  # cached via the getter
        # A NAME costs a search -> cache the resolution (the getter call inside is
        # cached too).
        return self._cached(
            "tvdb.record",
            f"resolve:{media}:{value}",
            lambda: self._resolve_name(value, media, getter),
            dump=_dump_item,
            load=MediaItem.model_validate_json,
        )

    def _resolve_name(self, value, media, getter) -> MediaItem:
        hits = self.search(str(value), media=media, limit=1)
        if not hits or hits[0].tvdb_id is None:
            raise ValueError(f"TVDB {media} not found: {value!r}")
        return getter(hits[0].tvdb_id, extended=True)

    # --- lists -------------------------------------------------------------

    def get_list(self, ref: int | str, *, media: str = "movie") -> list[MediaItem]:
        """A TVDB list's items for ``media`` (by id or slug), in the list's curated
        order.

        TV entities yield lightweight ``tvdb_id``-only items (enough for Sonarr +
        Jellyfin's Tvdb match); movie entities are fetched extended so they carry
        TMDB/IMDb ids.
        """
        return self._cached(
            "tvdb.list",
            f"list:{ref}:{media}",
            lambda: self._get_list(ref, media),
            dump=lambda xs: _LIST_ADAPTER.dump_json(xs).decode(),
            load=lambda s: _LIST_ADAPTER.validate_json(s),
        )

    def _get_list(self, ref: int | str, media: str) -> list[MediaItem]:
        if isinstance(ref, int) or str(ref).strip().isdigit():
            list_id: int | None = int(str(ref).strip())
        else:
            base = self._data(self.get(f"lists/slug/{ref}")) or {}
            list_id = base.get("id")
            if list_id is None:
                raise ValueError(f"TVDB list slug not found: {ref!r}")
        extended = self._data(self.get(f"lists/{list_id}/extended")) or {}
        key = "seriesId" if media == "tv" else "movieId"
        picked = sorted(
            (
                (e.get("order") or 0, e[key])
                for e in extended.get("entities") or []
                if e.get(key)
            ),
            key=lambda t: t[0],
        )
        if media == "tv":
            return [
                MediaItem(tvdb_id=int(eid), media_type="tv", title=f"tvdb-series-{eid}")
                for _, eid in picked
            ]
        return [self.get_movie(int(eid), extended=True) for _, eid in picked]

    # --- discover (filter) -------------------------------------------------

    def discover(
        self, filters: dict[str, Any], *, media: str = "tv", limit: int | None = None
    ) -> list[MediaItem]:
        """The ``/series|movies/filter`` discover endpoint, following pagination.

        ``country`` + ``lang`` are required by TVDB; defaults are filled in when absent.
        """
        return self._cached(
            "tvdb.query",
            self._discover_key(media, filters, limit),
            lambda: self._discover(filters, media, limit),
            dump=lambda xs: _LIST_ADAPTER.dump_json(xs).decode(),
            load=lambda s: _LIST_ADAPTER.validate_json(s),
        )

    def _discover_key(self, media: str, filters: dict[str, Any], limit) -> str:
        norm = "&".join(f"{k}={filters[k]}" for k in sorted(filters) if k != "page")
        return hashlib.sha1(f"{media}|{norm}|limit={limit}".encode()).hexdigest()

    def _discover(
        self, filters: dict[str, Any], media: str, limit: int | None
    ) -> list[MediaItem]:
        path = f"{'series' if media == 'tv' else 'movies'}/filter"
        params = {"country": _DEFAULT_COUNTRY, "lang": _DEFAULT_LANG, **dict(filters)}
        parse = self._series_item if media == "tv" else self._movie_item
        out: list[MediaItem] = []
        page = 0
        while True:
            payload = self.get(path, params={**params, "page": page}) or {}
            for rec in payload.get("data") or []:
                out.append(parse(rec))
                if limit is not None and len(out) >= limit:
                    return out[:limit]
            if not (payload.get("links") or {}).get("next"):
                return out
            page += 1
