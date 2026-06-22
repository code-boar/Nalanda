"""Language-code normalization, backed by the ``langcodes`` library.

A single seam for the differing code systems across metadata sources: TMDB uses
ISO 639-1 (2-letter, ``en``), TVDB ISO 639-2/T (3-letter, ``eng``), and the user's
``settings.language`` is a BCP-47 locale (``en-GB``). Everything is normalized to a
casefolded 2-letter subtag internally; helpers convert back out (e.g. ``to_alpha3`` for
TVDB). All wrappers degrade gracefully -- junk or unparseable input falls back to the
default rather than raising.
"""

from __future__ import annotations

import functools

import langcodes

_DEFAULT_SUBTAG = "en"
_DEFAULT_ALPHA3 = "eng"


@functools.cache
def to_subtag(tag: str | None, *, default: str = _DEFAULT_SUBTAG) -> str:
    """Normalize a locale/code to a canonical casefolded 2-letter subtag.

    ``en-GB`` -> ``en``, ``eng`` -> ``en``, ``EN_us`` -> ``en``. Falsy or unparseable
    input falls back to ``default``. Never raises.
    """
    if not tag:
        return default
    try:
        subtag = langcodes.Language.get(tag).language
    except ValueError, LookupError, AttributeError:  # incl. LanguageTagError
        return default
    return subtag.casefold() if subtag else default


@functools.cache
def to_alpha3(subtag: str | None, *, default: str = _DEFAULT_ALPHA3) -> str:
    """Convert a 2-letter subtag to ISO 639-2/T (``en`` -> ``eng``), for TVDB.
    Never raises.

    Not yet wired to a call site -- groundwork for routing ``settings.language`` to
    TVDB (which requires 3-letter codes) when per-title/franchise art lands there.
    """
    if not subtag:
        return default
    try:
        return langcodes.Language.get(subtag).to_alpha3()
    except ValueError, LookupError, AttributeError:  # incl. LanguageTagError
        return default


def image_language_param(subtag: str) -> str:
    """TMDB ``include_image_language`` value: preferred subtag + English + textless
    (``null``).

    Deduped order-preserving with ``dict.fromkeys`` (NOT a set): the order and dedup are
    load-bearing for the exact string TMDB is asked for -- ``en`` -> ``en,null``,
    ``fr`` -> ``fr,en,null``.
    """
    return ",".join(dict.fromkeys([subtag, "en", "null"]))
