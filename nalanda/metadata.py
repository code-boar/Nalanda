"""Pure planning for per-item Jellyfin metadata overrides.

Given a desired ``{config key: value}`` map and the item's current Jellyfin DTO, emit
the minimal set of changes to make the item match -- the field values plus the
``LockedFields`` needed so a metadata refresh cannot overwrite them. Idempotent: an
item already at the desired values with its locks in place produces an empty plan.

Only fields Jellyfin can individually lock are supported (its ``MetadataField`` enum),
plus ``sort_title``, which has no lock but is sticky via ``ForcedSortName`` (the same
mechanism collections use). ``ForcedSortName`` is not returned by GET, so any
read-modify-write that omits it nulls it; when an item has a managed ``sort_title``
and anything else is written, the plan re-asserts it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .sorting import expected_sort_name

# config key -> (Jellyfin DTO field, MetadataField lock enum or None for sort_title)
FIELD_MAP: dict[str, tuple[str, str | None]] = {
    "parental_rating": ("OfficialRating", "OfficialRating"),
    "title": ("Name", "Name"),
    "overview": ("Overview", "Overview"),
    "sort_title": ("ForcedSortName", None),
}


@dataclass
class ItemMetadataPlan:
    """The changes needed to make one item match its desired metadata."""

    changes: dict[str, Any] = field(default_factory=dict)
    updated: list[str] = field(
        default_factory=list
    )  # config keys that triggered a write

    @property
    def up_to_date(self) -> bool:
        return not self.changes

    def describe(self) -> str:
        return "up to date" if self.up_to_date else ", ".join(self.updated)


def plan_item_metadata(
    desired: dict[str, str], current_dto: dict[str, Any]
) -> ItemMetadataPlan:
    """Plan the writes to bring one item to ``desired`` (config key -> value).

    Keys in ``desired`` must be in ``FIELD_MAP`` (the config layer guarantees this).
    """
    plan = ItemMetadataPlan()
    current_locked = list(current_dto.get("LockedFields") or [])
    needed_locks: set[str] = set()

    for key, value in desired.items():
        dto_field, lock = FIELD_MAP[key]
        if dto_field == "ForcedSortName":
            # ForcedSortName isn't readable; compare its Jellyfin-computed SortName
            # instead.
            if expected_sort_name(value) != (current_dto.get("SortName") or ""):
                plan.changes[dto_field] = value
                plan.updated.append(key)
            continue
        lock_missing = lock is not None and lock not in current_locked
        if (current_dto.get(dto_field) or "") != value or lock_missing:
            plan.changes[dto_field] = value
            plan.updated.append(key)
            if lock is not None:
                needed_locks.add(lock)

    if needed_locks - set(current_locked):
        plan.changes["LockedFields"] = sorted(set(current_locked) | needed_locks)

    # Defensive re-assert: a write that omits ForcedSortName nulls it, so if this item
    # has a managed sort_title and we're writing anything, include it.
    if (
        "sort_title" in desired
        and plan.changes
        and "ForcedSortName" not in plan.changes
    ):
        plan.changes["ForcedSortName"] = desired["sort_title"]

    return plan


def plan_metadata_cleanup(
    orphan_fields: list[str], current_dto: dict[str, Any]
) -> dict[str, Any]:
    """Changes to unlock fields Nalanda previously managed but no longer declares.

    Lockable fields are dropped from ``LockedFields`` (the value is left in place for a
    future refresh to reclaim); a managed ``sort_title`` is cleared
    (``ForcedSortName`` -> "") so the item reverts to its computed sort name. Returns
    an empty dict when there is nothing to do.
    """
    changes: dict[str, Any] = {}
    current_locked = list(current_dto.get("LockedFields") or [])
    locks_to_drop = {
        FIELD_MAP[f][1] for f in orphan_fields if FIELD_MAP[f][1] is not None
    }
    if locks_to_drop & set(current_locked):
        changes["LockedFields"] = [
            lock for lock in current_locked if lock not in locks_to_drop
        ]
    if "sort_title" in orphan_fields:
        changes["ForcedSortName"] = ""
    return changes
