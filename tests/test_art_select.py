"""Unit tests for the provider-agnostic per-slot artwork selector."""

from __future__ import annotations

from nalanda.art_select import select_slot
from nalanda.models import ArtCandidate


def test_primary_single_candidate_is_returned():
    poster = ArtCandidate(slot="Primary", path="/poster.jpg")
    assert select_slot([poster], "Primary", preferred_lang="en") is poster


def test_backdrop_picks_best_scored_textless():
    low = ArtCandidate(slot="Backdrop", path="/low.jpg", has_text=False, score=(5.0, 3))
    high = ArtCandidate(
        slot="Backdrop", path="/high.jpg", has_text=False, score=(8.0, 9)
    )
    # textless: language is irrelevant, highest score wins regardless of order
    assert select_slot([low, high], "Backdrop", preferred_lang="en").path == "/high.jpg"
    assert select_slot([high, low], "Backdrop", preferred_lang="en").path == "/high.jpg"


def test_thumb_prefers_language_over_higher_score():
    en = ArtCandidate(
        slot="Thumb", path="/en.jpg", lang="en", has_text=True, score=(7.0, 50)
    )
    fr = ArtCandidate(
        slot="Thumb", path="/fr.jpg", lang="fr", has_text=True, score=(9.5, 99)
    )
    # preferred 'en' (lang tier 4) beats higher-rated 'fr' (tier 0)
    assert select_slot([en, fr], "Thumb", preferred_lang="en").path == "/en.jpg"


def test_thumb_english_tier_when_pref_absent():
    en = ArtCandidate(
        slot="Thumb", path="/en.jpg", lang="en", has_text=True, score=(6.0, 10)
    )
    fr = ArtCandidate(
        slot="Thumb", path="/fr.jpg", lang="fr", has_text=True, score=(9.0, 10)
    )
    # preferred 'es' has no candidate -> English (tier 3) beats other-lang (tier 0)
    assert select_slot([en, fr], "Thumb", preferred_lang="es").path == "/en.jpg"


def test_thumb_score_tiebreak_when_no_pref_or_english():
    fr = ArtCandidate(
        slot="Thumb", path="/fr.jpg", lang="fr", has_text=True, score=(6.0, 10)
    )
    de = ArtCandidate(
        slot="Thumb", path="/de.jpg", lang="de", has_text=True, score=(8.0, 10)
    )
    # neither preferred nor English -> both tier 0 -> rating tiebreak -> de
    assert select_slot([fr, de], "Thumb", preferred_lang="es").path == "/de.jpg"


def test_none_when_slot_absent():
    backdrop = ArtCandidate(
        slot="Backdrop", path="/b.jpg", has_text=False, score=(5.0, 1)
    )
    assert select_slot([backdrop], "Thumb", preferred_lang="en") is None
    assert select_slot([], "Primary", preferred_lang="en") is None


def test_first_maximal_candidate_wins_on_tie():
    first = ArtCandidate(
        slot="Thumb", path="/first.jpg", lang="en", has_text=True, score=(7.0, 50)
    )
    second = ArtCandidate(
        slot="Thumb", path="/second.jpg", lang="en", has_text=True, score=(7.0, 50)
    )
    # identical rank -> max keeps the first (source-order stability)
    assert (
        select_slot([first, second], "Thumb", preferred_lang="en").path == "/first.jpg"
    )
