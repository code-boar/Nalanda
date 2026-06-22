"""Unit tests for the langcodes-backed language normalization helpers."""

from __future__ import annotations

from nalanda.language import image_language_param, to_alpha3, to_subtag


def test_to_subtag_strips_region_and_normalizes_case():
    assert to_subtag("en-US") == "en"
    assert to_subtag("en-GB") == "en"
    assert to_subtag("EN-gb") == "en"
    assert to_subtag("fr-FR") == "fr"
    assert to_subtag("pt-BR") == "pt"


def test_to_subtag_accepts_three_letter_codes():
    # TVDB-style ISO 639-2/T codes normalize back to the 2-letter subtag.
    assert to_subtag("eng") == "en"
    assert to_subtag("fra") == "fr"


def test_to_subtag_falls_back_on_empty_or_garbage():
    assert to_subtag(None) == "en"
    assert to_subtag("") == "en"
    assert to_subtag("!!not a tag!!") == "en"  # must not raise
    assert to_subtag("zzzzzzzz") == "en"
    assert to_subtag("anything", default="de") == "de"


def test_to_alpha3_for_tvdb():
    assert to_alpha3("en") == "eng"
    assert to_alpha3("fr") == "fra"
    assert to_alpha3("pt") == "por"


def test_to_alpha3_falls_back_on_garbage():
    assert to_alpha3(None) == "eng"
    assert to_alpha3("!!nope!!") == "eng"  # must not raise


def test_image_language_param_dedupes_order_preserving():
    # English collapses to a single entry; textless `null` always last.
    assert image_language_param("en") == "en,null"
    assert image_language_param("fr") == "fr,en,null"
    assert image_language_param("pt") == "pt,en,null"


def test_to_subtag_is_memoized():
    to_subtag("en-CA")
    before = to_subtag.cache_info().hits
    to_subtag("en-CA")
    assert to_subtag.cache_info().hits >= before + 1
