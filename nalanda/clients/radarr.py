"""Radarr client -- movie specifics over :class:`ServarrClient`."""

from __future__ import annotations

from typing import Any

from ..models import RadarrMovie
from ._servarr import ServarrClient

# config minimum-availability values -> Radarr's API spelling.
_AVAILABILITY = {
    "announced": "announced",
    "in_cinemas": "inCinemas",
    "released": "released",
}


class RadarrClient(ServarrClient):
    LOOKUP_NS = "radarr.lookup"
    EMPTY_NS = "radarr.lookup_empty"
    FAILED_NS = "radarr.add_failed"

    def get_movies(self) -> list[RadarrMovie]:
        """Every movie Radarr tracks."""
        return [RadarrMovie.from_radarr(m) for m in self.get(f"{self.API}/movie")]

    def lookup_by_tmdb(self, tmdb_id: int) -> RadarrMovie | None:
        data = self.lookup_raw(tmdb_id)
        return RadarrMovie.from_radarr(data) if data else None

    def lookup_raw(self, tmdb_id: int) -> dict[str, Any] | None:
        """The RAW Radarr lookup object for a TMDB id -- the add-ready payload."""
        data = self.get(
            f"{self.API}/movie/lookup/tmdb",
            params={"tmdbId": tmdb_id},
            not_found_ok=True,
        )
        return data if isinstance(data, dict) and data else None

    def build_add_payload(
        self,
        tmdb_id: int,
        *,
        quality_profile_id: int,
        root_folder: str,
        monitored: bool = True,
        minimum_availability: str = "released",
        tag_ids: list[int] | None = None,
        search: bool = False,
    ) -> dict[str, Any] | None:
        payload = self._cached_lookup(
            f"tmdb:{tmdb_id}", lambda: self.lookup_raw(tmdb_id)
        )
        if payload is None:
            return None
        payload = dict(payload)
        payload["qualityProfileId"] = quality_profile_id
        payload["rootFolderPath"] = root_folder
        payload["monitored"] = monitored
        payload["minimumAvailability"] = _AVAILABILITY.get(
            minimum_availability, minimum_availability
        )
        payload["tags"] = list(tag_ids or [])
        payload["addOptions"] = {"searchForMovie": bool(search)}
        return payload

    def add_movie(self, tmdb_id: int, **kw: Any) -> RadarrMovie | None:
        payload = self.build_add_payload(tmdb_id, **kw)
        if payload is None:
            return None
        created = self.post(f"{self.API}/movie", json=payload)
        return (
            RadarrMovie.from_radarr(created)
            if isinstance(created, dict) and created
            else None
        )

    def add_movies(self, payloads: list[dict[str, Any]]) -> list[RadarrMovie]:
        """Bulk-add via ``POST /movie/import`` with a per-item 400 fallback."""
        return self._bulk_import(
            payloads,
            import_path="movie/import",
            single_path="movie",
            parse=RadarrMovie.from_radarr,
            noun="movie(s)",
        )

    def add_failed_recently(self, tmdb_id: int) -> bool:
        return self._add_failed_recently(f"tmdb:{tmdb_id}")

    def mark_add_failed(self, tmdb_id: int) -> None:
        self._mark_add_failed(f"tmdb:{tmdb_id}")

    def edit_movies(
        self,
        movie_ids: list[int],
        *,
        tags: list[int] | None = None,
        apply_tags: str | None = None,
        quality_profile_id: int | None = None,
        monitored: bool | None = None,
    ) -> None:
        """Bulk-edit movies (``PUT /movie/editor``). One logical edit per call."""
        body: dict[str, Any] = {}
        if tags is not None:
            body["tags"] = list(tags)
            body["applyTags"] = apply_tags or "add"
        if quality_profile_id is not None:
            body["qualityProfileId"] = quality_profile_id
        if monitored is not None:
            body["monitored"] = monitored
        self._editor_put(
            movie_ids, editor_path="movie/editor", id_key="movieIds", body=body
        )


def index_movies_by_tmdb(movies: list[RadarrMovie]) -> dict[int, RadarrMovie]:
    """Index Radarr movies by TMDB id -- the key collection movies are matched on."""
    return {m.tmdb_id: m for m in movies if m.tmdb_id is not None}
