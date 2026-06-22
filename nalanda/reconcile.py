"""Idempotent reconciliation planning for Jellyfin collections.

Pure logic: given the DESIRED state (ordered member ids + metadata) and the CURRENT
state, produce the minimal set of operations to make the collection match.

Jellyfin BoxSets have no move endpoint and ``add`` always appends to the end of
``LinkedChildren``, so order is fixed by the **longest-common-prefix** rule: keep
the agreeing prefix, then tear down and re-append the divergent tail. Worst case
(divergence at position 0) is a full rebuild; appending to the end is free.

Metadata (overview/poster) is set once and **locked** -- otherwise Jellyfin's own
provider identifies the BoxSet by name and overwrites it (often wrongly). Once our
overview is in place and "Overview" is locked, metadata is considered up to date.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .sorting import expected_sort_name

LOCKED_FIELDS = ["Name", "Overview"]

# The Jellyfin image slots we own, in apply order. Primary = poster, Thumb = landscape
# title card, Backdrop = fanart.
IMAGE_TYPES = ("Primary", "Thumb", "Backdrop")

# Collection `order:` config -> Jellyfin DisplayOrder. We ALWAYS keep LinkedChildren
# in source order (the written order == the source); DisplayOrder only changes how
# Jellyfin SORTS that for display. So switching modes is just a DisplayOrder change.
ORDER_MAP: dict[str, str] = {
    "source": "Default",
    "sort_name": "SortName",
    "release_date": "PremiereDate",
}


def _dedupe(ids: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _common_prefix_len(a: list[str], b: list[str]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


@dataclass
class CollectionPlan:
    """The operations needed to reconcile one collection."""

    name: str
    create: bool = False
    to_remove: list[str] = field(default_factory=list)
    to_add: list[str] = field(
        default_factory=list
    )  # IN ORDER, appended after the kept prefix
    set_display_order: str | None = None
    set_overview: str | None = None
    # image type -> url to download; and image types to clear. Keyed by IMAGE_TYPES.
    set_images: dict[str, str] = field(default_factory=dict)
    clear_images: list[str] = field(default_factory=list)
    lock_fields: list[str] = field(default_factory=list)
    set_lockdata: bool = False
    set_provider_ids: dict[str, str] | None = None
    clear_year: bool = (
        False  # empty ProductionYear + PremiereDate (a year on a whole set is noise)
    )
    # The collection's own ForcedSortName (for `sections`). `forced_sort_name` is the
    # desired value whenever one is configured; `set_forced_sort_name` is True only when
    # it is out of date and must be (re)written.
    forced_sort_name: str | None = None
    set_forced_sort_name: bool = False

    @property
    def up_to_date(self) -> bool:
        return not (
            self.create
            or self.to_remove
            or self.to_add
            or self.set_display_order
            or self.set_overview is not None
            or self.set_images
            or self.clear_images
            or self.lock_fields
            or self.set_lockdata
            or self.set_provider_ids is not None
            or self.set_forced_sort_name
            or self.clear_year
        )

    def describe(self) -> str:
        if self.up_to_date:
            return "up to date"
        bits: list[str] = []
        if self.create:
            bits.append("create")
        if self.to_remove:
            bits.append(f"-{len(self.to_remove)}")
        if self.to_add:
            bits.append(f"+{len(self.to_add)}")
        if self.set_display_order:
            bits.append(f"displayOrder={self.set_display_order}")
        if self.set_overview is not None:
            bits.append("overview")
        for image_type in self.set_images:
            bits.append(image_type.lower())
        for image_type in self.clear_images:
            bits.append(f"{image_type.lower()}-")
        if self.set_provider_ids is not None:
            bits.append("tmdb-id")
        if self.set_forced_sort_name:
            bits.append("sortName")
        if self.clear_year:
            bits.append("year-")
        return ", ".join(bits)


def plan_collection(
    *,
    name: str,
    desired_ids: list[str],
    current_ids: list[str] | None,
    sync_mode: str = "sync",
    current_display_order: str | None = None,
    desired_display_order: str = "Default",
    tmdb_id: int | None = None,
    desired_overview: str | None = None,
    desired_images: dict[str, str | None] | None = None,
    current_overview: str | None = None,
    current_locked: list[str] | None = None,
    current_tmdb_id: str | None = None,
    current_image_markers: dict[str, str | None] | None = None,
    current_image_types: set[str] | None = None,
    desired_forced_sort_name: str | None = None,
    current_sort_name: str | None = None,
    hide_year: bool = False,
    current_production_year: int | None = None,
    current_premiere_date: str | None = None,
) -> CollectionPlan:
    """Plan how to make a collection match the desired members (in order) + metadata.

    ``current_ids`` is ``None`` when the collection does not yet exist. ``sync_mode``
    is ``"sync"`` (collection becomes exactly ``desired_ids``) or ``"append"`` (only
    add missing desired items; never remove).
    """
    desired = _dedupe(desired_ids)
    exists = current_ids is not None
    current = _dedupe(current_ids or [])
    current_locked = current_locked or []

    if not exists and not desired:
        return CollectionPlan(name=name)  # nothing matched -> nothing to build

    create = not exists
    to_remove: list[str] = []
    to_add: list[str] = []

    if create:
        to_add = list(desired)
    elif sync_mode == "append":
        present = set(current)
        to_add = [i for i in desired if i not in present]
    elif desired_display_order != "Default":
        # Jellyfin OWNS the display order here (SortName/PremiereDate) and re-sorts the
        # stored children itself, so their stored order is not ours to control.
        # Reconcile membership as a SET (add missing, remove extra) -- comparing the
        # sequence would churn forever, fighting an order Jellyfin keeps rewriting.
        desired_set, current_set = set(desired), set(current)
        to_remove = [i for i in current if i not in desired_set]
        to_add = [i for i in desired if i not in current_set]
    else:
        # sync + Default display: the stored (insertion) order IS the displayed order,
        # so reconcile it precisely. Longest-common-prefix tear-down / re-append
        # (Jellyfin can only append, so a front divergence rebuilds the tail).
        k = _common_prefix_len(current, desired)
        to_remove = current[k:]
        to_add = desired[k:]

    set_do: str | None = (
        desired_display_order
        if create or current_display_order != desired_display_order
        else None
    )

    plan = CollectionPlan(
        name=name,
        create=create,
        to_remove=to_remove,
        to_add=to_add,
        set_display_order=set_do,
    )

    # Collection's own sort name (sections). We can't read ForcedSortName back, so we
    # compare the server's computed SortName against what Jellyfin would derive from the
    # desired ForcedSortName -- a write is needed only when they differ.
    plan.forced_sort_name = desired_forced_sort_name
    if desired_forced_sort_name is not None:
        if create or (current_sort_name or "") != expected_sort_name(
            desired_forced_sort_name
        ):
            plan.set_forced_sort_name = True

    # Metadata. We OWN it (set it ourselves from the fresh source each run; we don't let
    # Jellyfin's provider fetch it). Two INDEPENDENT decisions, each keyed only on
    # values the DTO actually returns -- IsLocked & ForcedSortName are NOT readable, so
    # they're never gates. LockData is set as a side effect of the (readable)
    # overview/lock block.
    desired_overview = desired_overview or ""
    tmdb_id_str = str(tmdb_id) if tmdb_id else None
    # A provider-id mismatch is only a trigger when there's a real single id to stamp:
    # stamping {"Tmdb": id} settles, but for a merge we must NOT fight a residual
    # auto-matched id every run (posting {} may not clear it) -- it's harmless under
    # LockData.
    needs_id = tmdb_id_str is not None and current_tmdb_id != tmdb_id_str
    if (
        create
        or (current_overview or "") != desired_overview
        or "Overview" not in current_locked
        or "Name" not in current_locked
        or needs_id
    ):
        plan.set_overview = desired_overview
        plan.lock_fields = list(LOCKED_FIELDS)  # ["Name", "Overview"]
        plan.set_lockdata = True  # stop Jellyfin's name-auto-match re-identifying it
        plan.set_provider_ids = {"Tmdb": tmdb_id_str} if tmdb_id_str else {}

    # Images (Primary/Thumb/Backdrop): the source isn't readable from Jellyfin (only a
    # post-download hash), so per slot we compare the desired URL against the
    # last-applied marker and re-pull only on a change (or if the image went missing).
    # MUST be if/else per slot -- a nested elif would clear then re-pull, flip-flopping
    # every run.
    # We own only the slots WE set: clearing is keyed on the marker (a URL we applied),
    # NOT on mere presence -- otherwise Jellyfin's own auto-images (it name-matches
    # locked BoxSets for images regardless of LockData) get deleted every run, churning
    # forever.
    desired_images = desired_images or {}
    markers = current_image_markers or {}
    present = current_image_types or set()
    for image_type in IMAGE_TYPES:
        desired_url = desired_images.get(image_type)
        if desired_url:
            if desired_url != markers.get(image_type) or image_type not in present:
                plan.set_images[image_type] = desired_url
        elif markers.get(image_type):
            plan.clear_images.append(
                image_type
            )  # we set one before, now removed -> clear

    # Year/release-date: Jellyfin derives ProductionYear + PremiereDate from members;
    # hide_year nulls both (kept null by LockData). Force on create (Jellyfin hasn't
    # derived yet but will); otherwise clear only when something is currently set.
    # hide_year=false leaves them alone.
    if hide_year and (
        create or current_production_year is not None or current_premiere_date
    ):
        plan.clear_year = True

    return plan
