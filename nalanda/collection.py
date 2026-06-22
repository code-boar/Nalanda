"""Build (reconcile) a Jellyfin collection from an ordered list of movies.

Ties the pieces together: match the source movies against the Jellyfin library to
get the desired ordered member ids, diff against the collection's current state
(members, order, and metadata) via :mod:`reconcile`, and apply the minimal changes.
Idempotent -- a second run with no underlying changes is a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .clients.jellyfin import JellyfinClient
from .logging import get_logger
from .matching import LibraryLookup
from .models import ImageSource, MediaItem
from .reconcile import ORDER_MAP, CollectionPlan, _dedupe, plan_collection

log = get_logger(__name__)


@dataclass
class BuildResult:
    name: str
    plan: CollectionPlan
    collection_id: str | None
    matched: int
    missing: int


def build_jellyfin_collection(
    jf: JellyfinClient,
    name: str,
    movies: list[MediaItem],
    library_index: LibraryLookup,
    *,
    sync_mode: str = "sync",
    order: str = "source",
    tmdb_collection_id: int | None = None,
    overview: str | None = None,
    desired_images: dict[str, ImageSource | None] | None = None,
    current_image_markers: dict[str, str | None] | None = None,
    forced_sort_name: str | None = None,
    hide_year: bool = False,
    apply: bool = True,
) -> BuildResult:
    """Reconcile the Jellyfin collection ``name`` to the matched movies + metadata."""
    display_order = ORDER_MAP.get(order, ORDER_MAP["source"])
    # Desired ordered member ids = source movies that exist in the library, in list
    # order.
    desired_ids: list[str] = []
    missing = 0
    for movie in movies:
        item = library_index.find(movie)
        if item is not None:
            desired_ids.append(item.id)
        else:
            missing += 1
    desired_ids = _dedupe(desired_ids)

    existing = jf.find_collection(name)
    current_ids: list[str] | None = None
    current_overview: str | None = None
    current_locked: list[str] | None = None
    current_display_order: str | None = None
    current_tmdb_id: str | None = None
    current_sort_name: str | None = None
    current_production_year: int | None = None
    current_premiere_date: str | None = None
    current_image_types: set[str] = set()
    if existing is not None:
        current_ids = [item.id for item in jf.get_collection_items(existing.id)]
        # Read the whole current state from one authoritative single-item DTO
        # (fresher and self-consistent vs. the get_collections list query).
        dto = jf.get_item(existing.id)
        current_overview = dto.get("Overview")
        current_locked = dto.get("LockedFields") or []
        current_display_order = dto.get("DisplayOrder")
        current_tmdb_id = (dto.get("ProviderIds") or {}).get("Tmdb")
        current_sort_name = dto.get(
            "SortName"
        )  # ForcedSortName isn't returned; compare via this
        current_production_year = dto.get("ProductionYear")
        current_premiere_date = dto.get("PremiereDate")
        # Which image slots currently exist. Primary/Thumb are in ImageTags; Backdrop
        # is a separate list (BackdropImageTags).
        image_tags = dto.get("ImageTags") or {}
        if image_tags.get("Primary"):
            current_image_types.add("Primary")
        if image_tags.get("Thumb"):
            current_image_types.add("Thumb")
        if dto.get("BackdropImageTags"):
            current_image_types.add("Backdrop")

    # The planner compares on marker strings only (URL, or "file:<hash>" for local
    # files); the ImageSource map is handed to _apply, which dispatches download vs.
    # upload per slot.
    sources = desired_images or {}
    desired_markers = {
        slot: (src.marker if src else None) for slot, src in sources.items()
    }

    plan = plan_collection(
        name=name,
        desired_ids=desired_ids,
        current_ids=current_ids,
        sync_mode=sync_mode,
        current_display_order=current_display_order,
        desired_display_order=display_order,
        tmdb_id=tmdb_collection_id,
        desired_overview=overview,
        desired_images=desired_markers,
        current_overview=current_overview,
        current_locked=current_locked,
        current_tmdb_id=current_tmdb_id,
        current_image_markers=current_image_markers,
        current_image_types=current_image_types,
        desired_forced_sort_name=forced_sort_name,
        current_sort_name=current_sort_name,
        hide_year=hide_year,
        current_production_year=current_production_year,
        current_premiere_date=current_premiere_date,
    )

    collection_id = existing.id if existing else None
    if apply and not plan.up_to_date:
        collection_id = _apply(jf, plan, sources, existing_id=collection_id)

    return BuildResult(
        name=name,
        plan=plan,
        collection_id=collection_id,
        matched=len(desired_ids),
        missing=missing,
    )


def _apply(
    jf: JellyfinClient,
    plan: CollectionPlan,
    sources: dict[str, ImageSource | None],
    *,
    existing_id: str | None,
) -> str | None:
    """Execute a plan against Jellyfin. Returns the collection id."""
    # 1) membership. Resolve the collection id first and bail if we don't have one,
    #    so the membership writes below never fire against a missing id.
    if plan.create:
        if not plan.to_add:
            return None
        # Seed with the first item, then append the rest in order (each add appends).
        cid = jf.create_collection(plan.name, [plan.to_add[0]])
        if not cid:
            return None
        if plan.to_add[1:]:
            jf.add_to_collection(cid, plan.to_add[1:])
    else:
        cid = existing_id
        if not cid:
            return None
        if plan.to_remove:
            jf.remove_from_collection(cid, plan.to_remove)
        if plan.to_add:
            jf.add_to_collection(cid, plan.to_add)

    # 2) metadata + display order (one read-modify-write). Clearing ProviderIds drops
    #    the bogus id Jellyfin auto-matched by name; locking stops it coming back.
    changes: dict[str, object] = {}
    if plan.set_display_order:
        changes["DisplayOrder"] = plan.set_display_order
    if plan.set_overview is not None:
        changes["Overview"] = plan.set_overview
    if plan.lock_fields:
        changes["LockedFields"] = plan.lock_fields
    if plan.set_lockdata:
        changes["LockData"] = (
            True  # stop Jellyfin auto-refresh re-identifying/overwriting it
        )
    if plan.set_provider_ids is not None:
        changes["ProviderIds"] = plan.set_provider_ids
    if (
        plan.clear_year
    ):  # a single year on a whole collection is noise; LockData keeps it empty
        changes["ProductionYear"] = None
        changes["PremiereDate"] = None
    # ForcedSortName isn't returned by GET, so update_item's read-modify-write would
    # null it. Re-assert it on ANY write to a sectioned collection, not just when it
    # changed.
    if plan.set_forced_sort_name or (plan.forced_sort_name is not None and changes):
        changes["ForcedSortName"] = plan.forced_sort_name
    if changes:
        jf.update_item(cid, changes)

    # 3) images: set our (config/repo/source) Primary/Thumb/Backdrop, or clear stray
    #    ones. plan.set_images only lists slots whose marker changed, so this never
    #    re-applies on a no-op run. Per slot we dispatch on the resolved source: a
    #    remote URL Jellyfin pulls itself, a local file's bytes we upload.
    for image_type in plan.set_images:
        # Jellyfin APPENDS backdrops (they're a list), so clear first to avoid pile-up.
        if image_type == "Backdrop":
            jf.delete_image(cid, image_type="Backdrop")
        src = sources.get(image_type)
        if src is None:
            continue
        if src.kind == "file":
            jf.upload_image(
                cid,
                Path(src.ref).read_bytes(),
                content_type=src.content_type or "image/jpeg",
                image_type=image_type,
            )
        else:
            jf.set_remote_image(cid, src.ref, image_type=image_type)
    for image_type in plan.clear_images:
        jf.delete_image(cid, image_type=image_type)
    return cid
