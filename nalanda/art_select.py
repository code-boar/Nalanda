"""Provider-agnostic per-slot artwork selection.

Given :class:`~nalanda.models.ArtCandidate` objects from any source, pick the best one
for a Jellyfin slot (Primary / Thumb / Backdrop) with a single ranking, so every
provider's art is chosen the same way. The ranking mirrors Jellyfin's
``OrderByLanguageDescending``: preferred language, then English, then the provider's own
score (e.g. rating, vote count).
"""

from __future__ import annotations

from collections.abc import Iterable

from .models import ArtCandidate


def select_slot(
    candidates: Iterable[ArtCandidate],
    slot: str,
    *,
    preferred_lang: str,
) -> ArtCandidate | None:
    """The best candidate for ``slot`` (or ``None`` if there are none of that slot).

    ``preferred_lang`` must already be canonical (a 2-letter subtag). Language only
    ranks *titled* candidates; textless art is ordered purely by ``score``. ``max``
    keeps the first maximal element, so callers must build candidates in source order
    for stable tie-breaks.
    """
    pool = [c for c in candidates if c.slot == slot]
    if not pool:
        return None

    def _lang_score(c: ArtCandidate) -> int:
        if not c.has_text:  # textless art: language is not part of the rank
            return 0
        code = (c.lang or "").casefold()
        return 4 if code == preferred_lang else 3 if code == "en" else 0

    return max(pool, key=lambda c: (_lang_score(c), *c.score))
