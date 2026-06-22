"""Unit tests for the pure collection reconciliation planner (no network)."""

from __future__ import annotations

from nalanda.reconcile import ORDER_MAP, plan_collection


def test_order_map():
    assert ORDER_MAP == {
        "source": "Default",
        "sort_name": "SortName",
        "release_date": "PremiereDate",
    }


def test_display_order_change_only():
    # members + order already correct; just flipping the display mode
    p = plan_collection(
        name="C",
        desired_ids=["A", "B"],
        current_ids=["A", "B"],
        current_display_order="Default",
        desired_display_order="PremiereDate",
    )
    assert p.to_add == []
    assert p.to_remove == []
    assert p.set_display_order == "PremiereDate"


def test_server_sorted_modes_dont_enforce_stored_order():
    # When DisplayOrder is server-managed (PremiereDate/SortName), Jellyfin owns
    # the order, so we do NOT rebuild on a reorder -- only membership is synced.
    # current [C,A] has the same members it should keep, so we just add the
    # missing B (no churn).
    p = plan_collection(
        name="C",
        desired_ids=["A", "B", "C"],
        current_ids=["C", "A"],
        current_display_order="PremiereDate",
        desired_display_order="PremiereDate",
    )
    assert p.to_remove == []
    assert p.to_add == ["B"]


def test_create_when_absent():
    p = plan_collection(name="C", desired_ids=["A", "B", "C"], current_ids=None)
    assert p.create is True
    assert p.to_add == ["A", "B", "C"]
    assert p.to_remove == []
    assert p.set_display_order == "Default"
    assert p.up_to_date is False


def test_forced_sort_name_idempotent_when_matching():
    # current SortName already equals Jellyfin's derivation of the desired
    # ForcedSortName; metadata is settled too, so the whole plan is a no-op
    # (proves no spurious write)
    p = plan_collection(
        name="C",
        desired_ids=["A"],
        current_ids=["A"],
        current_display_order="Default",
        desired_overview="",
        current_overview="",
        current_locked=["Name", "Overview"],
        desired_forced_sort_name="002 lord of rings",
        # digit-padded, as Jellyfin reports it
        current_sort_name="0000000002 lord of rings",
    )
    assert p.set_forced_sort_name is False
    assert p.up_to_date is True


def test_forced_sort_name_written_when_mismatched():
    p = plan_collection(
        name="C",
        desired_ids=["A"],
        current_ids=["A"],
        desired_forced_sort_name="002 lord of rings",
        current_sort_name="lord of rings",  # not yet sectioned
    )
    assert p.set_forced_sort_name is True
    assert p.forced_sort_name == "002 lord of rings"
    assert p.up_to_date is False


def test_forced_sort_name_carried_for_defensive_reassert():
    # even when the sort name already matches, it's remembered so an unrelated write
    # can re-include it (GET never returns ForcedSortName, so a bare RMW would null it)
    p = plan_collection(
        name="C",
        desired_ids=["A"],
        current_ids=["A"],
        desired_forced_sort_name="002 lord of rings",
        current_sort_name="0000000002 lord of rings",
    )
    assert (
        p.forced_sort_name == "002 lord of rings"
    )  # available to _apply's defensive branch
    assert p.set_forced_sort_name is False


def test_absent_with_no_matches_is_noop():
    p = plan_collection(name="C", desired_ids=[], current_ids=None)
    assert p.create is False
    assert p.up_to_date is True


def test_up_to_date():
    # up to date requires the overview to be set+locked too (else we'd manage it)
    p = plan_collection(
        name="C",
        desired_ids=["A", "B", "C"],
        current_ids=["A", "B", "C"],
        current_display_order="Default",
        current_overview="",
        current_locked=["Name", "Overview"],
    )
    assert p.up_to_date is True


def test_blank_overview_is_set_and_locked_to_suppress_automatch():
    # neither config nor source provides an overview -> still set "" and lock it
    p = plan_collection(
        name="C",
        desired_ids=["A"],
        current_ids=["A"],
        current_display_order="Default",
        current_overview=None,
        current_locked=[],
        current_image_types={"Primary"},  # a stray auto-matched poster present
    )
    assert p.set_overview == ""
    assert "Overview" in p.lock_fields
    assert "Primary" not in p.set_images
    # we only managed slots we set: a stray image we never applied is LEFT alone
    # (no churn)
    assert p.clear_images == []


def test_pure_append():
    p = plan_collection(
        name="C",
        desired_ids=["A", "B", "C", "D"],
        current_ids=["A", "B", "C"],
        current_display_order="Default",
    )
    assert p.to_remove == []
    assert p.to_add == ["D"]


def test_tail_rebuild_on_midlist_divergence():
    # current [A,B,X,C] vs desired [A,B,C,D]: common prefix [A,B]
    p = plan_collection(
        name="C",
        desired_ids=["A", "B", "C", "D"],
        current_ids=["A", "B", "X", "C"],
        current_display_order="Default",
    )
    assert p.to_remove == ["X", "C"]
    assert p.to_add == ["C", "D"]


def test_worst_case_front_change_is_full_rebuild():
    p = plan_collection(
        name="C",
        desired_ids=["Z", "A", "B"],
        current_ids=["A", "B"],
        current_display_order="Default",
    )
    assert p.to_remove == ["A", "B"]
    assert p.to_add == ["Z", "A", "B"]


def test_sync_removes_extras():
    p = plan_collection(
        name="C",
        desired_ids=["A", "C"],
        current_ids=["A", "B", "C"],
        current_display_order="Default",
    )
    assert p.to_remove == ["B", "C"]  # diverges at B
    assert p.to_add == ["C"]


def test_server_sorted_displayorder_ignores_member_reorder():
    # SortName/PremiereDate: Jellyfin owns the order, so the SAME members in a different
    # stored order reconcile to NO change -- the display-order churn fix.
    p = plan_collection(
        name="C",
        desired_ids=["A", "B", "C"],
        current_ids=["C", "B", "A"],  # Jellyfin re-sorted them behind our back
        desired_display_order="SortName",
        current_display_order="SortName",
        desired_overview="o",
        current_overview="o",
        current_locked=["Name", "Overview"],
    )
    assert p.to_remove == []
    assert p.to_add == []
    assert p.up_to_date is True


def test_server_sorted_displayorder_still_syncs_membership():
    # set-based, so a genuine membership change is still applied
    # (add missing, drop extra)
    p = plan_collection(
        name="C",
        desired_ids=["A", "B", "D"],
        current_ids=["C", "B", "A"],
        desired_display_order="PremiereDate",
        current_display_order="PremiereDate",
        desired_overview="o",
        current_overview="o",
        current_locked=["Name", "Overview"],
    )
    assert p.to_remove == ["C"]
    assert p.to_add == ["D"]


def test_default_displayorder_still_reorders_members():
    # contrast: with Default display the stored order IS the display, so a reorder
    # rebuilds
    p = plan_collection(
        name="C",
        desired_ids=["A", "B", "C"],
        current_ids=["C", "B", "A"],
        current_display_order="Default",
    )
    assert p.to_add == ["A", "B", "C"]  # front diverges -> full rebuild


def test_append_mode_never_removes():
    p = plan_collection(
        name="C",
        desired_ids=["A", "B", "C"],
        current_ids=["A", "X"],
        sync_mode="append",
        current_display_order="Default",
    )
    assert p.to_remove == []
    assert p.to_add == ["B", "C"]  # only missing desired items


def test_sets_display_order_when_wrong():
    p = plan_collection(
        name="C",
        desired_ids=["A", "B"],
        current_ids=["A", "B"],
        current_display_order="PremiereDate",
    )
    assert p.set_display_order == "Default"
    assert p.up_to_date is False


def test_dedupes_inputs():
    p = plan_collection(name="C", desired_ids=["A", "A", "B"], current_ids=None)
    assert p.to_add == ["A", "B"]


def test_create_sets_and_locks_metadata():
    p = plan_collection(
        name="C",
        desired_ids=["A"],
        current_ids=None,
        desired_overview="The real overview",
        desired_images={"Primary": "http://img/poster.jpg"},
    )
    assert p.set_overview == "The real overview"
    assert p.set_images["Primary"] == "http://img/poster.jpg"
    assert "Overview" in p.lock_fields


def test_metadata_up_to_date_when_matches_and_locked():
    p = plan_collection(
        name="C",
        desired_ids=["A"],
        current_ids=["A"],
        current_display_order="Default",
        desired_overview="Real",
        current_overview="Real",
        current_locked=["Name", "Overview"],
    )
    assert p.set_overview is None
    assert p.up_to_date is True


def test_metadata_reapplied_when_not_yet_locked():
    p = plan_collection(
        name="C",
        desired_ids=["A"],
        current_ids=["A"],
        current_display_order="Default",
        desired_overview="Real",
        current_overview="Real",  # matches but unlocked -> still (re)apply + lock
        current_locked=[],
    )
    assert p.set_overview == "Real"
    assert "Overview" in p.lock_fields


def test_single_collection_stamps_id_then_idempotent():
    # create -> stamp id + set/lock overview from the source; rerun once settled
    # -> no-op
    created = plan_collection(
        name="C",
        desired_ids=["A"],
        current_ids=None,
        tmdb_id=119,
        desired_overview="src",
    )
    assert created.set_provider_ids == {"Tmdb": "119"}
    assert created.set_overview == "src"
    assert created.lock_fields == ["Name", "Overview"]
    assert created.set_lockdata is True

    settled = plan_collection(
        name="C",
        desired_ids=["A"],
        current_ids=["A"],
        current_display_order="Default",
        tmdb_id=119,
        desired_overview="src",
        current_overview="src",
        current_locked=["Name", "Overview"],
        current_tmdb_id="119",
    )
    assert (
        settled.up_to_date is True
    )  # id present, overview matches + locked -> nothing


def test_we_own_stamps_tmdb_id_when_overriding():
    # config override on a single TMDB collection -> we own, but still stamp the id
    p = plan_collection(
        name="C",
        desired_ids=["A"],
        current_ids=None,
        tmdb_id=119,
        desired_overview="custom",
    )
    assert p.set_provider_ids == {"Tmdb": "119"}
    assert p.set_overview == "custom"
    assert "Overview" in p.lock_fields


# --- image marker idempotency (the image source isn't readable; we compare URLs) ---
_SETTLED = dict(  # a fully-settled metadata state, so only image logic varies
    current_display_order="Default",
    desired_overview="o",
    current_overview="o",
    current_locked=["Name", "Overview"],
)


def test_image_idempotent_when_marker_matches_and_image_present():
    p = plan_collection(
        name="C",
        desired_ids=["A"],
        current_ids=["A"],
        desired_images={"Primary": "http://x/p.jpg"},
        current_image_markers={"Primary": "http://x/p.jpg"},
        current_image_types={"Primary"},
        **_SETTLED,
    )
    assert "Primary" not in p.set_images
    assert not p.clear_images
    assert p.up_to_date is True  # full no-op -> guards the flip-flop bug


def test_image_repulled_when_url_changes():
    p = plan_collection(
        name="C",
        desired_ids=["A"],
        current_ids=["A"],
        desired_images={"Primary": "http://x/new.jpg"},
        current_image_markers={"Primary": "http://x/old.jpg"},
        current_image_types={"Primary"},
        **_SETTLED,
    )
    assert p.set_images["Primary"] == "http://x/new.jpg"


def test_image_repulled_when_image_missing_even_if_marker_matches():
    p = plan_collection(
        name="C",
        desired_ids=["A"],
        current_ids=["A"],
        desired_images={"Primary": "http://x/p.jpg"},
        current_image_markers={"Primary": "http://x/p.jpg"},
        current_image_types=set(),
        **_SETTLED,
    )
    assert p.set_images["Primary"] == "http://x/p.jpg"


def test_thumb_and_backdrop_are_independent_slots():
    # Primary settled, Thumb changed, Backdrop missing -> set Thumb + Backdrop only
    p = plan_collection(
        name="C",
        desired_ids=["A"],
        current_ids=["A"],
        desired_images={
            "Primary": "http://x/p.jpg",
            "Thumb": "http://x/new-thumb.jpg",
            "Backdrop": "http://x/bd.jpg",
        },
        current_image_markers={
            "Primary": "http://x/p.jpg",
            "Thumb": "http://x/old-thumb.jpg",
        },
        current_image_types={"Primary", "Thumb"},
        **_SETTLED,
    )
    assert "Primary" not in p.set_images
    assert p.set_images["Thumb"] == "http://x/new-thumb.jpg"
    assert p.set_images["Backdrop"] == "http://x/bd.jpg"  # marker absent + not present
    assert not p.clear_images


def test_blank_image_no_op_when_no_image():
    p = plan_collection(
        name="C",
        desired_ids=["A"],
        current_ids=["A"],
        desired_images={},
        current_image_markers={},
        current_image_types=set(),
        **_SETTLED,
    )
    assert not p.clear_images
    assert not p.set_images


def test_stray_jellyfin_image_is_left_alone():
    # Jellyfin auto-added a Thumb we never set (no marker). We must NOT clear it
    # -> no churn.
    p = plan_collection(
        name="C",
        desired_ids=["A"],
        current_ids=["A"],
        desired_images={"Primary": "http://x/p.jpg"},  # only Primary is ours
        current_image_markers={"Primary": "http://x/p.jpg"},  # no Thumb marker
        current_image_types={"Primary", "Thumb"},
        **_SETTLED,  # but a Thumb is present
    )
    assert not p.clear_images
    assert p.up_to_date is True


def test_removed_image_is_cleared():
    # We previously set a Thumb (marker present) but it's no longer desired -> clear it.
    p = plan_collection(
        name="C",
        desired_ids=["A"],
        current_ids=["A"],
        desired_images={"Primary": "http://x/p.jpg"},
        current_image_markers={"Primary": "http://x/p.jpg", "Thumb": "http://x/t.jpg"},
        current_image_types={"Primary", "Thumb"},
        **_SETTLED,
    )
    assert p.clear_images == ["Thumb"]
    assert p.up_to_date is False  # a clear is a write


def test_metadata_updated_when_overview_differs():
    p = plan_collection(
        name="C",
        desired_ids=["A"],
        current_ids=["A"],
        current_display_order="Default",
        desired_overview="Correct overview",
        current_overview="Wrong auto-matched text",
        current_locked=["Name", "Overview"],
    )
    assert p.set_overview == "Correct overview"


# --- hide_year: empty ProductionYear + PremiereDate (a year on a whole set is noise) -
def test_hide_year_forces_clear_on_create():
    # Jellyfin hasn't derived a year yet at create, but it will -> pre-empt it.
    p = plan_collection(name="C", desired_ids=["A"], current_ids=None, hide_year=True)
    assert p.clear_year is True


def test_hide_year_clears_when_year_present():
    p = plan_collection(
        name="C",
        desired_ids=["A"],
        current_ids=["A"],
        hide_year=True,
        current_production_year=1989,
        **_SETTLED,
    )
    assert p.clear_year is True
    assert p.up_to_date is False


def test_hide_year_noop_when_already_empty():
    p = plan_collection(
        name="C",
        desired_ids=["A"],
        current_ids=["A"],
        hide_year=True,
        current_production_year=None,
        current_premiere_date=None,
        **_SETTLED,
    )
    assert p.clear_year is False
    assert p.up_to_date is True  # already empty -> nothing to do (guards churn)


def test_hide_year_false_never_clears():
    p = plan_collection(
        name="C",
        desired_ids=["A"],
        current_ids=["A"],
        hide_year=False,
        current_production_year=1989,
        current_premiere_date="1989-06-20",
        **_SETTLED,
    )
    assert p.clear_year is False
