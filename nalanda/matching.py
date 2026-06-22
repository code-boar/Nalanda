"""Match resolved movies against what already exists in a media server.

Because Jellyfin's ``GET /Items`` can only filter by *whether* an item has a given
provider id (not by value), matching is done in memory: index the library's items
by ``(provider, id)`` once, then look each wanted movie up. This is also how the
real sync works -- build the index a single time and match every collection
against it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .logging import get_logger
from .models import JellyfinItem, MediaItem

log = get_logger(__name__)


class LibraryLookup(Protocol):
    """Anything that resolves a :class:`MediaItem` to a Jellyfin library item.

    Both :class:`LibraryIndex` (single media) and :class:`MediaRoutedIndex` (a mixed
    collection's per-media-type router) satisfy this -- it's what the collection builder
    matches against.
    """

    def find(self, item: MediaItem) -> JellyfinItem | None: ...


# Provider keys we index on (Jellyfin's PascalCase spelling). Movies usually carry
# Tmdb/Imdb, shows Tvdb (+ often Tmdb); `find` picks per item by which ids it has,
# Tvdb first.
MATCH_KEYS: tuple[str, ...] = ("Tmdb", "Imdb", "Tvdb")


@dataclass
class MatchResult:
    """The outcome of matching a set of movies against a library."""

    matched: list[tuple[MediaItem, JellyfinItem]] = field(default_factory=list)
    missing: list[MediaItem] = field(default_factory=list)

    @property
    def matched_movies(self) -> list[MediaItem]:
        return [movie for movie, _ in self.matched]


class LibraryIndex:
    """An in-memory index of Jellyfin items keyed by ``(provider, id)``."""

    def __init__(self, items: list[JellyfinItem]) -> None:
        self._by_provider: dict[tuple[str, str], JellyfinItem] = {}
        for item in items:
            for key in MATCH_KEYS:
                value = item.provider_ids.get(key)
                if value:
                    # First write wins -- avoids a later duplicate shadowing the first.
                    self._by_provider.setdefault((key, value), item)
        self.size = len(items)

    def find(self, item: MediaItem) -> JellyfinItem | None:
        """Find the library item for a media item, trying TVDB, then TMDB, then IMDb.

        Tvdb is consulted first (shows' primary id); movies normally have no tvdb_id and
        fall straight through to Tmdb/Imdb, so ordering is harmless for them.
        """
        if item.tvdb_id is not None:
            hit = self._by_provider.get(("Tvdb", str(item.tvdb_id)))
            if hit:
                return hit
        if item.tmdb_id is not None:
            hit = self._by_provider.get(("Tmdb", str(item.tmdb_id)))
            if hit:
                return hit
        if item.imdb_id:
            hit = self._by_provider.get(("Imdb", item.imdb_id))
            if hit:
                return hit
        return None

    def match(self, movies: list[MediaItem]) -> MatchResult:
        """Split ``movies`` into those present in the library and those missing."""
        result = MatchResult()
        for movie in movies:
            hit = self.find(movie)
            if hit is not None:
                result.matched.append((movie, hit))
            else:
                result.missing.append(movie)
        return result


class MediaRoutedIndex:
    """Routes ``find`` to a per-``media_type`` :class:`LibraryIndex`.

    A mixed collection matches its movie items against the movie library index and its
    show items against the show index. Keeping the two indexes separate is what stops a
    movie and a show that share a numeric TMDB id from cross-matching (the movie and TV
    TMDB id spaces are distinct).
    """

    def __init__(self, by_media: dict[str, LibraryIndex]) -> None:
        self._by_media = by_media
        self.size = sum(index.size for index in by_media.values())

    def find(self, item: MediaItem) -> JellyfinItem | None:
        index = self._by_media.get(item.media_type)
        return index.find(item) if index is not None else None
