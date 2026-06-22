"""Shared base for the Radarr and Sonarr clients (the *arr family).

Both speak the same Servarr API: ``/api/v3`` under an ``X-Api-Key`` header, identical
quality-profile / tag / root-folder / system-status reads, the same ``ensure_tag``
create-if-absent, the same negative-cached metadata lookup, the same
bulk-import-with-400-fallback add, and the same batched editor PUT. This base holds all
of that; the subclasses supply only the per-service specifics (lookup shape, model
parser, endpoint nouns, and the extras TV has).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ..cache import Cache
from ..http import BaseClient, HTTPError
from ..logging import get_logger
from ..models import ArrQualityProfile, ArrRootFolder, ArrTag

log = get_logger(__name__)

EDITOR_BATCH = 100  # /editor and /import accept many ids; batch to be safe.


class ServarrClient(BaseClient):
    """Base for :class:`RadarrClient` / :class:`SonarrClient`.

    Subclasses set the three cache-namespace constants (their values differ so Radarr
    and Sonarr never share a cache row).
    """

    API = "api/v3"
    # Subclasses MUST set these (values differ so Radarr/Sonarr never share a cache
    # row).
    LOOKUP_NS: str
    EMPTY_NS: str
    FAILED_NS: str

    def __init__(
        self, base_url: str, api_key: str, *, cache: Cache | None = None, **kwargs: Any
    ) -> None:
        if not base_url or not api_key:
            raise ValueError(f"{type(self).__name__} base URL and API key are required")
        self._cache = cache
        super().__init__(base_url, headers={"X-Api-Key": api_key}, **kwargs)

    # --- shared reads --------------------------------------------------------

    def get_system_status(self) -> dict[str, Any]:
        """Server status (appName, version) -- also a cheap auth check."""
        return self.get(f"{self.API}/system/status")

    def get_quality_profiles(self) -> list[ArrQualityProfile]:
        return [
            ArrQualityProfile.from_arr(p)
            for p in self.get(f"{self.API}/qualityprofile")
        ]

    def get_tags(self) -> list[ArrTag]:
        return [ArrTag.from_arr(t) for t in self.get(f"{self.API}/tag")]

    def get_root_folders(self) -> list[ArrRootFolder]:
        return [ArrRootFolder.from_arr(r) for r in self.get(f"{self.API}/rootfolder")]

    def ensure_tag(self, label: str) -> int:
        """Return the id of the tag with ``label``, creating it if absent
        (case-insensitive)."""
        wanted = label.casefold()
        for tag in self.get_tags():
            if tag.label.casefold() == wanted:
                return tag.id
        created = self.post(f"{self.API}/tag", json={"label": label})
        return int(created["id"])

    # --- negative-cached metadata lookup -------------------------------------

    def _cached_lookup(
        self, key: str, loader: Callable[[], dict[str, Any] | None]
    ) -> dict[str, Any] | None:
        """``loader()`` result for ``key``, cache-backed with a short-TTL negative
        cache.

        ``key`` is the full cache key (e.g. ``"tmdb:550"`` / ``"term:tvdb:1"``). The
        payload is source metadata, so a repeated add of the same id never re-hits
        Radarr/Sonarr's lookup endpoint; an unknown id is negative-cached so a
        later-published title is still picked up once the short TTL lapses.
        """
        if self._cache is None:
            return loader()
        cached = self._cache.read(self.LOOKUP_NS, key)
        if cached is not None:
            return json.loads(cached)
        if self._cache.read(self.EMPTY_NS, key) is not None:
            return None
        result = loader()
        if result is None:
            self._cache.write(self.EMPTY_NS, key, "1")
        else:
            self._cache.write(self.LOOKUP_NS, key, json.dumps(result))
        return result

    def _add_failed_recently(self, key: str) -> bool:
        return (
            self._cache is not None
            and self._cache.read(self.FAILED_NS, key) is not None
        )

    def _mark_add_failed(self, key: str) -> None:
        if self._cache is not None:
            self._cache.write(self.FAILED_NS, key, "1")

    # --- bulk import (per-item 400 fallback) ---------------------------------

    def _bulk_import(
        self,
        payloads: list[dict[str, Any]],
        *,
        import_path: str,
        single_path: str,
        parse: Callable[[dict[str, Any]], Any],
        noun: str,
    ) -> list[Any]:
        """POST ``payloads`` to ``import_path`` in batches. The import endpoint is
        all-or-nothing, so a 400 falls back to per-item ``single_path`` POSTs (valid
        items still land, each rejection isolated). ``parse`` maps a response dict to a
        model.
        """
        added: list[Any] = []
        for start in range(0, len(payloads), EDITOR_BATCH):
            chunk = payloads[start : start + EDITOR_BATCH]
            try:
                result = self.post(f"{self.API}/{import_path}", json=chunk)
            except HTTPError as exc:
                if exc.status != 400:
                    raise
                log.warning(
                    "%s bulk import returned 400; falling back to per-item adds"
                    " for %d %s",
                    type(self).__name__,
                    len(chunk),
                    noun,
                )
                for payload in chunk:
                    try:
                        created = self.post(f"{self.API}/{single_path}", json=payload)
                    except HTTPError:
                        continue  # invalid item -> caller marks it failed (not in
                        # `added`)
                    if isinstance(created, dict) and created:
                        added.append(parse(created))
                continue
            if isinstance(result, list):
                added.extend(parse(x) for x in result if isinstance(x, dict))
        return added

    # --- batched editor PUT --------------------------------------------------

    def _editor_put(
        self, ids: list[int], *, editor_path: str, id_key: str, body: dict[str, Any]
    ) -> None:
        """PUT ``body`` merged with ``{id_key: chunk}`` to ``editor_path`` in
        id-batches."""
        clean = [int(i) for i in ids]
        if not clean or not body:
            return
        for start in range(0, len(clean), EDITOR_BATCH):
            chunk = clean[start : start + EDITOR_BATCH]
            self.put(f"{self.API}/{editor_path}", json={id_key: chunk, **body})
