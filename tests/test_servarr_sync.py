"""Offline tests for the shared sync core (plan reconciliation + plan describe)."""

from __future__ import annotations

from dataclasses import dataclass, field

from nalanda._servarr_sync import BasePlan, reconcile_existing


def test_resolve_named_id():
    from nalanda._servarr_sync import resolve_named_id

    assert resolve_named_id(None, set(), {}) is None
    assert resolve_named_id(4, {4}, {}) == 4  # int already a known id
    assert resolve_named_id("HD", set(), {"hd": 1}) == 1  # case-insensitive name
    assert resolve_named_id("nope", set(), {"hd": 1}) is None  # unknown -> None
    assert resolve_named_id(9, set(), {}) is None  # int not a known id, no name match


def test_apply_adds_marks_reject_even_when_key_is_none():
    """A rejected add whose payload has no join key must still be marked failed.

    Sonarr keys adds on ``tvdbId``; if a created series carries no ``tvdb_id`` then
    ``got`` contains ``None``, which must not be allowed to mask a separate reject whose
    payload key is also ``None`` (otherwise that reject retries every run, uncached).
    """
    from types import SimpleNamespace

    from nalanda._servarr_sync import apply_adds

    marked: list[str] = []
    apply_adds(
        ["added", "rejected"],
        build_payload=lambda token: (
            {"tvdbId": 7} if token == "added" else {"title": token}  # reject: no tvdbId
        ),
        failed_recently=lambda token: False,
        mark_failed=lambda token: marked.append(token),
        # Sonarr returns the real add (7) plus a tvdb-less item -> got == {7, None}.
        bulk_add=lambda payloads: [
            SimpleNamespace(tvdb_id=7),
            SimpleNamespace(tvdb_id=None),
        ],
        added_key=lambda s: s.tvdb_id,
        payload_key=lambda token, payload: payload.get("tvdbId"),
        noun="series",
    )
    assert marked == ["rejected"]  # the real add (tvdbId 7) is not marked


@dataclass
class _Opts:
    add_missing: bool = False
    add_existing: bool = False
    upgrade_existing: bool = False
    monitor_existing: bool = False
    stale_tags: str = "mark"


@dataclass
class _Item:
    id: int | None
    key: int
    tags: list[int] = field(default_factory=list)
    quality_profile_id: int | None = None
    monitored: bool = False


def test_base_plan_describe_and_adds_dedup():
    p = BasePlan(name="C", adds=[1, 2], tag_add=[10])
    assert p.up_to_date is False
    assert p.describe() == "add 2, tag+1"
    assert BasePlan(name="C").up_to_date is True
    assert BasePlan(name="C").describe() == "up to date"

    universe: dict = {}
    desired = [_Item(id=None, key=1), _Item(id=None, key=1), _Item(id=None, key=2)]
    _, fields = reconcile_existing(
        name="C",
        desired=desired,
        matcher=lambda it: universe.get(it.key),
        universe=universe.values(),
        add_token=lambda it: it.key,
        opts=_Opts(add_missing=True),
        identity_tag_id=7,
        stale_tag_id=None,
        quality_profile_id=None,
        monitored_value=True,
    )
    assert fields["adds"] == [1, 2]  # deduped by token, order-preserved


def test_reconcile_marks_departed_and_adopts_present():
    a = _Item(id=10, key=1, tags=[7])  # present + already tagged
    b = _Item(id=11, key=2, tags=[7])  # tagged but no longer desired -> departed
    universe = {1: a, 2: b}
    desired = [_Item(id=None, key=1)]
    present, fields = reconcile_existing(
        name="C",
        desired=desired,
        matcher=lambda it: universe.get(it.key),
        universe=universe.values(),
        add_token=lambda it: it.key,
        opts=_Opts(add_existing=True, stale_tags="delete"),
        identity_tag_id=7,
        stale_tag_id=None,
        quality_profile_id=None,
        monitored_value=True,
    )
    plan = BasePlan(**fields)
    assert set(present) == {10}
    assert plan.tag_remove == [11]  # departed holder
    assert plan.tag_add == []  # 10 already tagged
    assert plan.describe() == "tag-1"
