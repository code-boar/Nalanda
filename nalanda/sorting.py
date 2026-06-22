"""Jellyfin sort-name emulation, for the ``sections`` feature.

Jellyfin orders collections by their ``SortName``. To group collections into ordered
*sections* we set a BoxSet's ``ForcedSortName`` to ``"<NNN> <normalized name>"``. But
Jellyfin does **not** run its article/character stripping on a *forced* sort name -- it
only applies :func:`_modify_sort_chunks` (digit padding) and lower-cases it. So to stay
consistent with how Jellyfin sorts everything else, we replicate ``CreateSortName``'s
normalization ourselves here.

Ported from Jellyfin ``MediaBrowser.Controller/Entities/BaseItem.cs``
(``CreateSortName`` + ``ModifySortChunks``). The three word/character lists are
server-configurable; the defaults below match a stock server and we read the live values
from ``/System/Configuration`` at runtime.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable

# Jellyfin ServerConfiguration defaults (MediaBrowser.Model/Configuration).
DEFAULT_REMOVE_WORDS = ["the", "a", "an"]
DEFAULT_REMOVE_CHARS = [",", "&", "-", "{", "}", "'"]
DEFAULT_REPLACE_CHARS = [".", "+", "%"]

SortableFn = Callable[[str], str]


def jellyfin_sortable(
    name: str,
    *,
    remove_words: list[str] = DEFAULT_REMOVE_WORDS,
    remove_chars: list[str] = DEFAULT_REMOVE_CHARS,
    replace_chars: list[str] = DEFAULT_REPLACE_CHARS,
) -> str:
    """Normalise a name the way Jellyfin's ``CreateSortName`` does (minus the digit
    chunking, which Jellyfin applies later via :func:`_modify_sort_chunks`).

    Lower-case, strip the article words at start / middle / end (so "The Lord of the
    Rings" -> "lord of rings"), delete the remove-characters, replace the
    replace-characters with a space.

    Each article is removed in a single non-overlapping pass (a plain ``str.replace``),
    exactly as Jellyfin does -- so adjacent articles ("the the x") only lose one. Keep
    it that way: recursing would diverge from the server's ``SortName`` and break the
    :func:`expected_sort_name` idempotency comparison.
    """
    sortable = name.strip().lower()
    for word in remove_words:
        if sortable.startswith(word + " "):
            sortable = sortable[len(word) + 1 :]
        sortable = sortable.replace(" " + word + " ", " ")
        if sortable.endswith(" " + word):
            sortable = sortable[: -(len(word) + 1)]
    for char in remove_chars:
        sortable = sortable.replace(char, "")
    for char in replace_chars:
        sortable = sortable.replace(char, " ")
    return sortable


def _modify_sort_chunks(name: str) -> str:
    """Port of Jellyfin ``ModifySortChunks``: zero-pad each run of digits to 10 chars
    (so numeric runs sort naturally) and strip diacritics."""
    if not name:
        return ""
    chunks: list[str] = []
    start = 0
    index = 0
    length = len(name)
    while index < length:
        is_digit = name[index].isdigit()
        end = index
        while end < length and name[end].isdigit() == is_digit:
            end += 1
        chunk = name[start:end]
        if is_digit and len(chunk) < 10:
            chunk = "0" * (10 - len(chunk)) + chunk
        chunks.append(chunk)
        start = end
        index = end
    result = "".join(chunks)
    return "".join(
        c for c in unicodedata.normalize("NFKD", result) if not unicodedata.combining(c)
    )


def expected_sort_name(forced_sort_name: str) -> str:
    """The ``SortName`` Jellyfin will compute from a ``ForcedSortName``.

    Jellyfin does ``ModifySortChunks(ForcedSortName).ToLowerInvariant()`` -- so we can
    compare this against the server's reported ``SortName`` to decide, without a write,
    whether a collection's forced sort name is already correct.
    """
    return _modify_sort_chunks(forced_sort_name).lower()


def section_prefix(index: int, total: int) -> str:
    """The zero-padded 1-based prefix for the ``index``-th of ``total`` sections."""
    width = max(3, len(str(total)))
    return f"{index + 1:0{width}d}"


def build_forced_sort_name(
    name: str,
    *,
    prefix: str | None,
    sort_title: str | None,
    sortable: SortableFn,
) -> str | None:
    """Combine a section prefix and/or an explicit sort title into a ForcedSortName.

    Four cases:
      * no section, no sort_title -> ``None`` (don't force; keep Jellyfin's native sort)
      * sort_title only           -> the sort_title verbatim (manual override)
      * section only              -> ``"<prefix> <normalized name>"``
      * section + sort_title      -> ``"<prefix> <sort_title>"`` (custom within-section
        order)
    """
    if prefix is None and sort_title is None:
        return None
    if prefix is None:
        return sort_title
    base = sort_title if sort_title is not None else sortable(name)
    return f"{prefix} {base}"
