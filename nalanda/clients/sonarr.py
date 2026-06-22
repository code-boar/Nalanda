"""Sonarr client -- series specifics over :class:`ServarrClient` (language profiles,
per-season monitoring, the extra add/edit fields TV has).
"""

from __future__ import annotations

from typing import Any

from ..models import SonarrLanguageProfile, SonarrSeries
from ._servarr import ServarrClient

# config monitor strategy -> Sonarr's addOptions/seasonpass spelling.
_MONITOR = {
    "all": "all",
    "future": "future",
    "missing": "missing",
    "existing": "existing",
    "pilot": "pilot",
    "first_season": "firstSeason",
    "latest_season": "latestSeason",
    "none": "none",
}


def sonarr_monitor(value: str) -> str:
    """Map a friendly monitor strategy to Sonarr's API spelling
    (identity if unknown)."""
    return _MONITOR.get(value, value)


class SonarrClient(ServarrClient):
    LOOKUP_NS = "sonarr.lookup"
    EMPTY_NS = "sonarr.lookup_empty"
    FAILED_NS = "sonarr.add_failed"

    def get_language_profiles(self) -> list[SonarrLanguageProfile]:
        """Language profiles (Sonarr v3). v4 removed the endpoint -> returns ``[]``
        on 404."""
        data = self.get(f"{self.API}/languageprofile", not_found_ok=True)
        return (
            [SonarrLanguageProfile.from_sonarr(p) for p in data]
            if isinstance(data, list)
            else []
        )

    def get_series(self) -> list[SonarrSeries]:
        """Every series Sonarr tracks."""
        return [SonarrSeries.from_sonarr(s) for s in self.get(f"{self.API}/series")]

    def get_series_raw(self, series_id: int) -> dict[str, Any] | None:
        """The RAW series object (for read-modify-write of season monitoring)."""
        data = self.get(f"{self.API}/series/{series_id}", not_found_ok=True)
        return data if isinstance(data, dict) and data else None

    def lookup(self, term: str) -> list[dict[str, Any]]:
        """Raw series lookup results for a term
        (``tvdb:NNN`` / ``tmdb:NNN`` / ``imdb:ttNNN``)."""
        data = self.get(
            f"{self.API}/series/lookup", params={"term": term}, not_found_ok=True
        )
        return data if isinstance(data, list) else []

    def _lookup_first(self, term: str) -> dict[str, Any] | None:
        matches = self.lookup(term)
        return dict(matches[0]) if matches else None

    def build_add_payload(
        self,
        term: str,
        *,
        quality_profile_id: int,
        root_folder: str,
        language_profile_id: int | None = None,
        monitored: bool = True,
        monitor: str = "all",
        series_type: str = "standard",
        season_folder: bool = True,
        tag_ids: list[int] | None = None,
        search: bool = False,
        cutoff_search: bool = False,
    ) -> dict[str, Any] | None:
        payload = self._cached_lookup(f"term:{term}", lambda: self._lookup_first(term))
        if payload is None:
            return None
        payload = dict(payload)
        payload["qualityProfileId"] = quality_profile_id
        if language_profile_id is not None:
            payload["languageProfileId"] = language_profile_id
        payload["rootFolderPath"] = root_folder
        payload["monitored"] = monitored
        payload["seriesType"] = series_type
        payload["seasonFolder"] = season_folder
        payload["tags"] = list(tag_ids or [])
        payload["addOptions"] = {
            "monitor": sonarr_monitor(monitor),
            "searchForMissingEpisodes": bool(search),
            "searchForCutoffUnmetEpisodes": bool(cutoff_search),
        }
        return payload

    def add_series(self, term: str, **kw: Any) -> SonarrSeries | None:
        payload = self.build_add_payload(term, **kw)
        if payload is None:
            return None
        created = self.post(f"{self.API}/series", json=payload)
        return (
            SonarrSeries.from_sonarr(created)
            if isinstance(created, dict) and created
            else None
        )

    def add_series_bulk(self, payloads: list[dict[str, Any]]) -> list[SonarrSeries]:
        """Bulk-add via ``POST /series/import`` with a per-item 400 fallback."""
        return self._bulk_import(
            payloads,
            import_path="series/import",
            single_path="series",
            parse=SonarrSeries.from_sonarr,
            noun="series",
        )

    def add_failed_recently(self, term: str) -> bool:
        return self._add_failed_recently(f"term:{term}")

    def mark_add_failed(self, term: str) -> None:
        self._mark_add_failed(f"term:{term}")

    def edit_series(
        self,
        series_ids: list[int],
        *,
        tags: list[int] | None = None,
        apply_tags: str | None = None,
        quality_profile_id: int | None = None,
        monitored: bool | None = None,
        series_type: str | None = None,
        season_folder: bool | None = None,
    ) -> None:
        """Bulk-edit series (``PUT /series/editor``). One logical edit per call."""
        body: dict[str, Any] = {}
        if tags is not None:
            body["tags"] = list(tags)
            body["applyTags"] = apply_tags or "add"
        if quality_profile_id is not None:
            body["qualityProfileId"] = quality_profile_id
        if monitored is not None:
            body["monitored"] = monitored
        if series_type is not None:
            body["seriesType"] = series_type
        if season_folder is not None:
            body["seasonFolder"] = season_folder
        self._editor_put(
            series_ids, editor_path="series/editor", id_key="seriesIds", body=body
        )

    def set_seasons(self, series_id: int, monitored_by_number: dict[int, bool]) -> bool:
        """Set explicit per-season ``monitored`` flags
        (read-modify-write ``PUT /series/{id}``)."""
        raw = self.get_series_raw(series_id)
        if raw is None:
            return False
        changed = False
        for season in raw.get("seasons") or []:
            number = season.get("seasonNumber")
            if (
                number in monitored_by_number
                and bool(season.get("monitored")) != monitored_by_number[number]
            ):
                season["monitored"] = monitored_by_number[number]
                changed = True
        if changed:
            self.put(f"{self.API}/series/{series_id}", json=raw)
        return changed


def index_series_by_ids(
    series: list[SonarrSeries],
) -> dict[str, dict[Any, SonarrSeries]]:
    """Index series by each id space:
    ``{"tvdb": {...}, "tmdb": {...}, "imdb": {...}}``."""
    index: dict[str, dict[Any, SonarrSeries]] = {"tvdb": {}, "tmdb": {}, "imdb": {}}
    for s in series:
        if s.tvdb_id is not None:
            index["tvdb"].setdefault(s.tvdb_id, s)
        if s.tmdb_id is not None:
            index["tmdb"].setdefault(s.tmdb_id, s)
        if s.imdb_id:
            index["imdb"].setdefault(s.imdb_id, s)
    return index
