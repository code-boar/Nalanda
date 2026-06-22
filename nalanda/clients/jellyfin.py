"""Jellyfin client.

Read: system info, library discovery, item retrieval with provider ids.
Write: collection (BoxSet) create / add / remove and item delete -- used to build
collections and (currently) to probe Jellyfin's collection-ordering behaviour.

Auth uses Jellyfin's official scheme -- an ``Authorization`` header of the form
``MediaBrowser Token="<api key>"``.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from typing import Any

from ..http import BaseClient
from ..logging import get_logger
from ..models import JellyfinCollection, JellyfinItem, JellyfinLibrary

log = get_logger(__name__)

_PAGE_SIZE = 1000

# Collection member ids travel in the `ids` query string; a large collection (e.g. a
# 250-title list) would overflow the server's URL-length limit (HTTP 414), so members
# are added/removed in batches. 50 ids (~1.7 KB of URL) stays well under typical
# proxy/Kestrel request-line limits.
_ITEM_BATCH = 50


def _batched(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class JellyfinClient(BaseClient):
    def __init__(self, base_url: str, api_key: str, **kwargs: Any) -> None:
        if not base_url or not api_key:
            raise ValueError("Jellyfin base URL and API key are required")
        headers = {"Authorization": f'MediaBrowser Token="{api_key}"'}
        # Jellyfin write/refresh operations on large collections (a 250-title BoxSet,
        # image downloads) can take well over the 30s default, especially on a remote
        # server -- give it more headroom so a slow metadata write doesn't abort the
        # run.
        kwargs.setdefault("timeout", 120.0)
        super().__init__(base_url, headers=headers, **kwargs)
        self._user_id: str | None = None

    @property
    def user_id(self) -> str | None:
        """The first (admin) user's id -- some endpoints require a user context."""
        if self._user_id is None:
            users = self.get("Users")
            self._user_id = users[0]["Id"] if users else None
        return self._user_id

    # --- server / libraries ------------------------------------------------

    def get_system_info(self) -> dict[str, Any]:
        """Server info (name, version) -- also a cheap auth check."""
        return self.get("System/Info")

    def get_sort_settings(self) -> tuple[list[str], list[str], list[str]]:
        """The server's sort word/character lists ``(remove_words, remove_chars,
        replace_chars)`` from ``/System/Configuration``, falling back to Jellyfin's
        defaults. Used to reproduce its sort-name normalisation exactly."""
        from ..sorting import (
            DEFAULT_REMOVE_CHARS,
            DEFAULT_REMOVE_WORDS,
            DEFAULT_REPLACE_CHARS,
        )

        try:
            cfg = self.get("System/Configuration")
        except Exception:  # admin-only endpoint / older server -> use defaults
            cfg = {}
        return (
            cfg.get("SortRemoveWords") or DEFAULT_REMOVE_WORDS,
            cfg.get("SortRemoveCharacters") or DEFAULT_REMOVE_CHARS,
            cfg.get("SortReplaceCharacters") or DEFAULT_REPLACE_CHARS,
        )

    def get_libraries(self) -> list[JellyfinLibrary]:
        data = self.get("Library/VirtualFolders")
        return [JellyfinLibrary.from_jellyfin(lib) for lib in data]

    def resolve_libraries(
        self, *, collection_type: str | None = None
    ) -> list[JellyfinLibrary]:
        """All libraries, optionally filtered to one content type
        (``movies``/``tvshows``)."""
        libraries = self.get_libraries()
        if collection_type is not None:
            libraries = [
                lib for lib in libraries if lib.collection_type == collection_type
            ]
        return libraries

    # --- items -------------------------------------------------------------

    def iter_items(
        self,
        *,
        parent_id: str | None = None,
        include_item_types: tuple[str, ...] = ("Movie",),
        fields: tuple[str, ...] = ("ProviderIds",),
        page_size: int = _PAGE_SIZE,
    ) -> Iterator[JellyfinItem]:
        """Yield items recursively, following pagination."""
        start = 0
        while True:
            params: dict[str, Any] = {
                "recursive": "true",
                "includeItemTypes": ",".join(include_item_types),
                "fields": ",".join(fields),
                "startIndex": start,
                "limit": page_size,
                "enableTotalRecordCount": "true",
                "sortBy": "SortName",
            }
            if parent_id is not None:
                params["parentId"] = parent_id
            data = self.get("Items", params=params)
            batch = data.get("Items", [])
            for raw in batch:
                yield JellyfinItem.from_jellyfin(raw)
            start += len(batch)
            if not batch or start >= int(data.get("TotalRecordCount") or 0):
                break

    def get_movies(self, library_ids: list[str] | None = None) -> list[JellyfinItem]:
        """All movies, optionally restricted to the given library ids."""
        if not library_ids:
            return list(self.iter_items())
        movies: list[JellyfinItem] = []
        for library_id in library_ids:
            movies.extend(self.iter_items(parent_id=library_id))
        return movies

    def get_series(self, library_ids: list[str] | None = None) -> list[JellyfinItem]:
        """All series (shows), optionally restricted to the given library ids.

        The TV analogue of :meth:`get_movies` -- a collection (BoxSet) holds whole
        ``Series`` items, matched and assembled exactly like movies.
        """
        if not library_ids:
            return list(self.iter_items(include_item_types=("Series",)))
        series: list[JellyfinItem] = []
        for library_id in library_ids:
            series.extend(
                self.iter_items(parent_id=library_id, include_item_types=("Series",))
            )
        return series

    # --- collections (discovery + write) ----------------------------------

    def get_collections(self) -> list[JellyfinCollection]:
        """All collections (BoxSets) on the server, with child count + display order."""
        data = self.get(
            "Items",
            params={
                "includeItemTypes": "BoxSet",
                "recursive": "true",
                "userId": self.user_id,
                "fields": "ChildCount,DisplayOrder",
            },
        )
        return [JellyfinCollection.from_jellyfin(raw) for raw in data.get("Items", [])]

    def find_collection(self, name: str) -> JellyfinCollection | None:
        """Find an existing collection by (case-insensitive) name, or None."""
        target = name.strip().casefold()
        matches = [
            c for c in self.get_collections() if c.name.strip().casefold() == target
        ]
        if len(matches) > 1:
            log.warning(
                "Multiple Jellyfin collections named %r; using the first.", name
            )
        return matches[0] if matches else None

    def get_collection_items(
        self,
        collection_id: str,
        *,
        sort_by: str | None = None,
        fields: tuple[str, ...] = ("ProviderIds",),
    ) -> list[JellyfinItem]:
        """Children of a collection. With no ``sort_by`` this returns them in
        ``LinkedChildren`` (insertion) order -- the order they display under
        ``DisplayOrder: "Default"`` and the order reconciliation compares against.
        """
        params: dict[str, Any] = {
            "parentId": collection_id,
            "recursive": "true",  # BoxSet children come back empty without this
            "fields": ",".join(fields),
        }
        if sort_by:
            params["sortBy"] = sort_by
        data = self.get("Items", params=params)
        return [JellyfinItem.from_jellyfin(raw) for raw in data.get("Items", [])]

    def create_collection(
        self, name: str, item_ids: list[str] | None = None
    ) -> str | None:
        """Create a BoxSet (optionally seeded with items); returns its id."""
        params: dict[str, Any] = {"name": name}
        if item_ids:
            params["ids"] = ",".join(item_ids)
        result = self.post("Collections", params=params)
        return result.get("Id") if isinstance(result, dict) else None

    def add_to_collection(self, collection_id: str, item_ids: list[str]) -> None:
        # Batched: all ids in one query string overflows the URL limit for large
        # collections.
        for batch in _batched(item_ids, _ITEM_BATCH):
            self.post(
                f"Collections/{collection_id}/Items", params={"ids": ",".join(batch)}
            )

    def remove_from_collection(self, collection_id: str, item_ids: list[str]) -> None:
        for batch in _batched(item_ids, _ITEM_BATCH):
            self.delete(
                f"Collections/{collection_id}/Items", params={"ids": ",".join(batch)}
            )

    def delete_item(self, item_id: str) -> None:
        """Delete an item (e.g. a collection). Irreversible."""
        self.delete(f"Items/{item_id}")

    def get_item(self, item_id: str) -> dict[str, Any]:
        """Fetch a single item's full DTO (for read-modify-write updates)."""
        return self.get(f"Users/{self.user_id}/Items/{item_id}")

    def update_item(self, item_id: str, changes: dict[str, Any]) -> None:
        """Read-modify-write an item's metadata (e.g. set DisplayOrder)."""
        dto = self.get_item(item_id)
        dto.update(changes)
        self.post(f"Items/{item_id}", json=dto)

    def refresh_item(
        self,
        item_id: str,
        *,
        metadata_mode: str = "Default",
        image_mode: str = "None",
        replace_metadata: bool = False,
        replace_images: bool = False,
    ) -> None:
        """Queue a metadata/image refresh for an item (async on the server)."""
        self.post(
            f"Items/{item_id}/Refresh",
            params={
                "metadataRefreshMode": metadata_mode,
                "imageRefreshMode": image_mode,
                "replaceAllMetadata": str(replace_metadata).lower(),
                "replaceAllImages": str(replace_images).lower(),
            },
        )

    def set_remote_image(
        self, item_id: str, image_url: str, *, image_type: str = "Primary"
    ) -> None:
        """Have Jellyfin download a remote image (e.g. a TMDB poster URL) and set it."""
        self.post(
            f"Items/{item_id}/RemoteImages/Download",
            params={"type": image_type, "imageUrl": image_url},
        )

    def upload_image(
        self,
        item_id: str,
        data: bytes,
        *,
        content_type: str,
        image_type: str = "Primary",
    ) -> None:
        """Upload local image bytes to an item's slot (e.g. a poster read off disk).

        Jellyfin's upload endpoint takes the image **base64-encoded** in the body with
        the original Content-Type. (A remote URL uses :meth:`set_remote_image` instead,
        which has Jellyfin fetch it directly.)
        """
        self.post_bytes(
            f"Items/{item_id}/Images/{image_type}",
            base64.b64encode(data),
            content_type=content_type,
        )

    def delete_image(self, item_id: str, *, image_type: str = "Primary") -> None:
        """Remove an item's image (e.g. a wrongly auto-matched poster). Best-effort."""
        self.request_json(
            "DELETE", f"Items/{item_id}/Images/{image_type}", not_found_ok=True
        )
