"""Shared core for declarative Radarr/Sonarr reconciliation.

Both engines diff a collection's desired members against the *arr's current state into a
plan, then apply it (or, with ``dry_run``, log it). The identity tag tracks DESIRED
membership; departing items follow the three-state stale policy; re-joiners have their
stale tag cleared. Everything here is media-agnostic -- the engines supply a matcher,
an add-token, the existing-items universe, and the desired monitored flag; Sonarr adds
season monitoring on top.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import _ResolvedArr
from .logging import get_logger
from .tagging import build_identity_tag

log = get_logger(__name__)


class _Existing(Protocol):
    """The shared shape of a RadarrMovie / SonarrSeries the core reads."""

    id: int | None
    tags: list[int]
    quality_profile_id: int | None
    monitored: bool


@dataclass
class BasePlan:
    """The writes shared by both engines. Ids are *arr ids; ``adds`` are add-tokens."""

    name: str
    adds: list[Any] = field(
        default_factory=list
    )  # tmdb ids (Radarr) / lookup terms (Sonarr)
    tag_add: list[int] = field(default_factory=list)
    tag_remove: list[int] = field(default_factory=list)
    stale_add: list[int] = field(default_factory=list)
    stale_remove: list[int] = field(default_factory=list)
    profile_updates: list[int] = field(default_factory=list)
    monitor_updates: list[int] = field(default_factory=list)

    def _parts(self) -> list[str]:
        parts: list[str] = []
        if self.adds:
            parts.append(f"add {len(self.adds)}")
        if self.tag_add:
            parts.append(f"tag+{len(self.tag_add)}")
        if self.tag_remove:
            parts.append(f"tag-{len(self.tag_remove)}")
        if self.stale_add:
            parts.append(f"stale+{len(self.stale_add)}")
        if self.stale_remove:
            parts.append(f"stale-{len(self.stale_remove)}")
        if self.profile_updates:
            parts.append(f"qp~{len(self.profile_updates)}")
        if self.monitor_updates:
            parts.append(f"mon~{len(self.monitor_updates)}")
        return parts

    @property
    def up_to_date(self) -> bool:
        return not self._parts()

    def describe(self) -> str:
        return ", ".join(self._parts()) or "up to date"


def reconcile_existing(
    *,
    name: str,
    desired: list[Any],
    matcher: Callable[[Any], _Existing | None],
    universe: Iterable[_Existing],
    add_token: Callable[[Any], Any],
    opts: Any,
    identity_tag_id: int,
    stale_tag_id: int | None,
    quality_profile_id: int | None,
    monitored_value: bool,
) -> tuple[dict[int, Any], dict[str, Any]]:
    """The plan core. Returns ``(present-by-arr-id, common plan fields)``.

    ``matcher`` finds the existing *arr item for a desired item (or None);
    ``universe`` is every existing item to scan for departed identity-tag holders;
    ``add_token(item)`` returns the add key for a desired item (tmdb id / lookup term),
    or ``None`` if the
    item is unresolvable; ``monitored_value`` is the desired item-level monitored flag
    (``opts.monitor`` for Radarr, ``opts.monitored`` for Sonarr).
    """
    present: dict[int, Any] = {}
    for item in desired:
        ex = matcher(item)
        if ex is not None and ex.id is not None:
            present.setdefault(ex.id, ex)
    desired_present_ids = set(present)

    adds: list[Any] = []
    if opts.add_missing:
        seen: set[Any] = set()
        for item in desired:
            if matcher(item) is not None:
                continue
            tok = add_token(item)
            if tok is not None and tok not in seen:
                seen.add(tok)
                adds.append(tok)

    tag_add: list[int] = []
    stale_remove: list[int] = []
    for xid, ex in present.items():
        has_identity = identity_tag_id in ex.tags
        has_stale = stale_tag_id is not None and stale_tag_id in ex.tags
        if has_stale:
            stale_remove.append(xid)  # rejoin: always clear our own stale tag
        if not has_identity and (opts.add_existing or has_stale):
            tag_add.append(xid)

    tag_remove: list[int] = []
    stale_add: list[int] = []
    for ex in universe:
        if (
            ex.id is None
            or identity_tag_id not in ex.tags
            or ex.id in desired_present_ids
        ):
            continue
        if opts.stale_tags == "delete":
            tag_remove.append(ex.id)
        elif opts.stale_tags == "mark":
            tag_remove.append(ex.id)
            # stale_tag_id is None on a first-ever mark: no item can already hold it,
            # so every departed item needs it. The caller creates the tag lazily once
            # stale_add is set.
            if stale_tag_id is None or stale_tag_id not in ex.tags:
                stale_add.append(ex.id)
        # "keep" -> leave the identity tag in place

    profile_updates: list[int] = []
    if opts.upgrade_existing and quality_profile_id is not None:
        profile_updates = [
            xid
            for xid, ex in present.items()
            if ex.quality_profile_id != quality_profile_id
        ]

    monitor_updates: list[int] = []
    if opts.monitor_existing:
        monitor_updates = [
            xid for xid, ex in present.items() if ex.monitored != monitored_value
        ]

    fields = dict(
        name=name,
        adds=adds,
        tag_add=tag_add,
        tag_remove=tag_remove,
        stale_add=stale_add,
        stale_remove=stale_remove,
        profile_updates=profile_updates,
        monitor_updates=monitor_updates,
    )
    return present, fields


def apply_adds(
    adds: list[Any],
    *,
    build_payload: Callable[[Any], dict[str, Any] | None],
    failed_recently: Callable[[Any], bool],
    mark_failed: Callable[[Any], None],
    bulk_add: Callable[[list[dict[str, Any]]], list[Any]],
    added_key: Callable[[Any], Any],
    payload_key: Callable[[Any, dict[str, Any]], Any],
    noun: str,
) -> None:
    """Build add payloads (sequential, cache-backed lookups), bulk-import, then mark
    the rejected ones. ``added_key`` reads the identity of a created item;
    ``payload_key`` reads that identity from the add result -- directly from the token
    (Radarr: ``lambda tok, _: tok``) or from the payload
    (Sonarr: ``lambda _, p: p.get('tvdbId')``) -- so rejects can be detected. A key
    that resolves to ``None`` is treated as unconfirmed (marked failed), so a missing
    id can't mask a reject.
    """
    payloads: list[tuple[Any, dict[str, Any]]] = []
    unresolved: list[Any] = []
    for token in adds:
        if failed_recently(token):
            continue  # a recent add was rejected -> skip until the marker expires
        payload = build_payload(token)
        if payload is None:
            unresolved.append(token)
        else:
            payloads.append((token, payload))
    if unresolved:
        log.warning(
            "        could not add %d %s (no lookup): %s",
            len(unresolved),
            noun,
            unresolved,
        )
    if not payloads:
        return
    added = bulk_add([p for _, p in payloads])
    got = {added_key(x) for x in added}
    for token, payload in payloads:
        key = payload_key(token, payload)
        # A None key can't confirm the add (e.g. a series with no tvdbId); treat it as
        # unconfirmed so it never matches another None in `got` and mask a real reject.
        if key is None or key not in got:
            mark_failed(token)


def apply_tag_writes(
    edit: Callable[..., None],
    plan: BasePlan,
    *,
    identity_tag_id: int,
    stale_tag_id: int | None,
    quality_profile_id: int | None,
    monitored_value: bool,
) -> None:
    """The shared tag/stale/profile/monitor editor calls. ``edit`` is
    ``client.edit_movies`` / ``client.edit_series`` (same keyword contract)."""
    if plan.tag_add:
        edit(plan.tag_add, tags=[identity_tag_id], apply_tags="add")
    if plan.tag_remove:
        edit(plan.tag_remove, tags=[identity_tag_id], apply_tags="remove")
    if stale_tag_id is not None and plan.stale_add:
        edit(plan.stale_add, tags=[stale_tag_id], apply_tags="add")
    if stale_tag_id is not None and plan.stale_remove:
        edit(plan.stale_remove, tags=[stale_tag_id], apply_tags="remove")
    if quality_profile_id is not None and plan.profile_updates:
        edit(plan.profile_updates, quality_profile_id=quality_profile_id)
    if plan.monitor_updates:
        edit(plan.monitor_updates, monitored=monitored_value)


def resolve_named_id(
    value: str | int | None, ids: set[int], by_name: dict[str, int]
) -> int | None:
    """Resolve a profile reference (id or name) to its id, or None if unset/unknown.

    An int already present in ``ids`` is taken as-is; otherwise the value is matched
    case-insensitively against ``by_name``.
    """
    if value is None:
        return None
    if isinstance(value, int) and value in ids:
        return value
    return by_name.get(str(value).casefold())


class _ArrCtx(Protocol):
    """Context fields prepare_reconcile reads (Radarr/SonarrContext satisfy this)."""

    profile_ids: set[int]
    profile_id_by_name: dict[str, int]
    root_folder_paths: set[str]
    tag_id_by_label: dict[str, int]


def prepare_reconcile(
    client: Any,
    name: str,
    *,
    opts: _ResolvedArr,
    ctx: _ArrCtx,
    service: str,
    dry_run: bool,
) -> tuple[int | None, int, str, int | None]:
    """Shared reconcile preamble: resolve the quality profile, validate, and resolve
    the identity + stale tag ids. Returns ``(quality_profile_id, identity_tag_id,
    stale_label, stale_tag_id)``. Raises ValueError on a missing profile / root folder.
    ``service`` is the capitalised name ("Radarr"/"Sonarr") used in log lines. On a dry
    run the ``identity_tag_id`` is ``-1`` when the tag does not yet exist (an id no item
    holds).
    """
    qp_id = resolve_named_id(
        opts.quality_profile, ctx.profile_ids, ctx.profile_id_by_name
    )
    if (opts.add_missing or opts.upgrade_existing) and qp_id is None:
        raise ValueError(
            f"quality_profile {opts.quality_profile!r} not found in {service}"
        )
    if opts.add_missing and not opts.root_folder:
        raise ValueError("add_missing requires a root_folder")
    if (
        opts.root_folder
        and ctx.root_folder_paths
        and opts.root_folder not in ctx.root_folder_paths
    ):
        log.warning(
            "  %-32s %s root_folder %r not among %s",
            name,
            service.lower(),
            opts.root_folder,
            sorted(ctx.root_folder_paths),
        )
    identity_label = build_identity_tag(name, tag_prefix=opts.tag_prefix, tag=opts.tag)
    identity_tag_id = (
        client.ensure_tag(identity_label)
        if not dry_run
        else ctx.tag_id_by_label.get(identity_label.casefold(), -1)
    )
    stale_label = f"{identity_label}{opts.stale_suffix}"
    stale_tag_id = ctx.tag_id_by_label.get(stale_label.casefold())
    return qp_id, identity_tag_id, stale_label, stale_tag_id


def finalize_reconcile(
    client: Any,
    name: str,
    plan: BasePlan,
    *,
    opts: _ResolvedArr,
    stale_label: str,
    stale_tag_id: int | None,
    service: str,
    dry_run: bool,
) -> int | None:
    """Lazily create the stale tag if a mark is pending, log the plan line, and return
    the (possibly newly created) stale_tag_id for apply."""
    if (
        not dry_run
        and opts.stale_tags == "mark"
        and plan.stale_add
        and stale_tag_id is None
    ):
        stale_tag_id = client.ensure_tag(stale_label)
    log.info("  %-32s %s: %s", name, service.lower(), plan.describe())
    return stale_tag_id


def sweep(
    client: Any,
    *,
    list_attr: str,
    edit_attr: str,
    service: str,
    noun: str,
    identity_label: str,
    stale_label: str,
    policy: str,
    dry_run: bool,
) -> None:
    """Apply the stale policy to ALL holders of a collection's identity tag.

    ``list_attr``/``edit_attr`` name the client's per-service list/edit methods
    (``"get_movies"``/``"edit_movies"`` for Radarr,
    ``"get_series"``/``"edit_series"`` for Sonarr); they are resolved only after the
    keep short-circuit, so a keep-policy caller's client need not expose them.
    """
    if policy == "keep":
        return
    tags = {t.label.casefold(): t.id for t in client.get_tags()}
    identity_id = tags.get(identity_label.casefold())
    if identity_id is None:
        return  # never tagged (or used a custom override we can't see)
    get_all = getattr(client, list_attr)
    edit = getattr(client, edit_attr)
    holders = [x.id for x in get_all() if x.id is not None and identity_id in x.tags]
    if not holders:
        return
    if dry_run:
        log.info(
            "    [dry-run] %s sweep %s: %d %s -> %s",
            service,
            identity_label,
            len(holders),
            noun,
            policy,
        )
        return
    edit(holders, tags=[identity_id], apply_tags="remove")
    if policy == "mark":
        stale_id = client.ensure_tag(stale_label)
        edit(holders, tags=[stale_id], apply_tags="add")
