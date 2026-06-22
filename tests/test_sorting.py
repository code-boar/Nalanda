"""Unit tests for the Jellyfin sort-name emulation (the `sections` feature)."""

from __future__ import annotations

from nalanda.sorting import (
    build_forced_sort_name,
    expected_sort_name,
    jellyfin_sortable,
    section_prefix,
)


def test_jellyfin_sortable_strips_articles_everywhere():
    # leading AND internal "the" go (matches Jellyfin's CreateSortName, verified live)
    assert jellyfin_sortable("The Best of the Year") == "best of year"
    assert jellyfin_sortable("Tale of a City") == "tale of city"  # internal "a"
    assert jellyfin_sortable("A1") == "a1"  # "a" only strips as a whole word


def test_jellyfin_sortable_matches_jellyfin_for_adjacent_articles():
    # Jellyfin removes each article in a single non-overlapping pass, so consecutive
    # articles only lose ONE. We match that quirk deliberately: recursing would diverge
    # from the server's SortName and break expected_sort_name's idempotency check.
    assert jellyfin_sortable("The The Title") == "the title"  # only leading "the" goes
    assert jellyfin_sortable("A The Thing") == "thing"  # middle "the", then leading "a"


def test_jellyfin_sortable_character_rules():
    assert jellyfin_sortable("Cook's Book") == "cooks book"  # apostrophe removed
    assert jellyfin_sortable("Made-Up") == "madeup"  # hyphen removed
    assert jellyfin_sortable("This & That") == "this  that"  # ampersand removed


def test_jellyfin_sortable_respects_custom_word_list():
    # the server's lists are passed in; an empty list strips nothing
    assert jellyfin_sortable("The Sample", remove_words=[]) == "the sample"


def test_expected_sort_name_zero_pads_digits_and_strips_diacritics():
    # Jellyfin pads each digit run to 10 chars and removes diacritics
    assert expected_sort_name("002 sample title") == "0000000002 sample title"
    assert expected_sort_name("003 sample title") == "0000000003 sample title"
    assert expected_sort_name("002 café bar") == "0000000002 cafe bar"  # é -> e


def test_section_prefix_padding():
    assert section_prefix(0, 3) == "001"
    assert section_prefix(1, 3) == "002"
    assert section_prefix(9, 12) == "010"
    assert section_prefix(0, 1000) == "0001"  # widens past 3 digits when needed


def test_build_forced_sort_name_four_cases():
    sortable = jellyfin_sortable
    # 1. nothing -> None (don't force a sort name)
    assert (
        build_forced_sort_name(
            "The Sample", prefix=None, sort_title=None, sortable=sortable
        )
        is None
    )
    # 2. sort_title only -> verbatim
    assert (
        build_forced_sort_name(
            "The Sample", prefix=None, sort_title="zzz", sortable=sortable
        )
        == "zzz"
    )
    # 3. section only -> "<prefix> <normalized name>"
    assert (
        build_forced_sort_name(
            "The Sample", prefix="002", sort_title=None, sortable=sortable
        )
        == "002 sample"
    )
    # 4. section + sort_title -> "<prefix> <sort_title>" (custom within-section order)
    assert (
        build_forced_sort_name(
            "The Sample", prefix="002", sort_title="01", sortable=sortable
        )
        == "002 01"
    )
