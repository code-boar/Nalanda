"""The ``nalanda serve`` webhook daemon.

A small stdlib ``http.server`` exposing two POST routes split by blast radius, plus a
health check:

* ``POST /trigger`` -- reactive and ALWAYS scoped. A Radarr/Sonarr webhook carrying
  ``nalanda-`` identity tags (or an explicit ``{"collections": [...]}`` body) runs just
  those collections. An empty / unrecognised / tag-less body is a 204 no-op; this route
  can never run everything.
* ``POST /run`` -- explicit full run, gated by ``webhook.allow_full_run`` (else 403).
* ``GET /health`` -- unauthenticated liveness, no side effects.

Both POST routes require the shared secret in the ``X-Nalanda-Token`` header
(constant-time compared). A debounce window coalesces a burst of triggers into one run,
and a single run-lock guarantees runs never overlap (they mutate Jellyfin + the state
file). Internal cron schedules (the ``settings.run_schedules`` /
``settings.run_schedule`` / ``settings.jobs`` cascade) fire scoped or default runs from
inside the daemon (not gated by ``allow_full_run`` -- they're local config, not inbound
requests).
"""

from __future__ import annotations

import hmac
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from croniter import croniter

from .config import Secrets, effective_radarr, effective_sonarr, load_config
from .logging import get_logger
from .radarr_sync import identity_tag_label
from .sonarr_sync import identity_tag_label as sonarr_identity_tag_label

log = get_logger(__name__)

TOKEN_HEADER = "X-Nalanda-Token"
MAX_BODY = 1_048_576  # 1 MiB cap on request bodies
ALL = "ALL"  # scope sentinel for a full run


def resolve_scope(
    body: dict[str, Any],
    *,
    collection_names: set[str],
    tag_to_collection: dict[str, str],
) -> set[str] | None:
    """Resolve a ``/trigger`` payload to a set of collection names, or ``None`` (no-op).

    Looks for identity tags in a Radarr (``movie.tags``) / Sonarr (``series.tags``)
    webhook, or a top-level ``tags``, mapping each KNOWN identity tag back to its
    collection. Falls back to an explicit ``collections`` list (validated against
    config). Anything else -> ``None``. This never yields a full run -- that's
    ``/run``'s job.
    """
    tags: list[Any] = []
    for key in ("movie", "series"):
        node = body.get(key)
        if isinstance(node, dict):
            tags += node.get("tags") or []
    tags += body.get("tags") or []
    matched = {
        tag_to_collection[t.casefold()]
        for t in tags
        if isinstance(t, str) and t.casefold() in tag_to_collection
    }
    if matched:
        return matched
    cols = body.get("collections")
    if isinstance(cols, list):
        valid = {c for c in cols if c in collection_names}
        if valid:
            return valid
    return None


def next_cron(cron_expr: str, base: datetime) -> datetime:
    """The next datetime a cron expression fires after ``base`` (pure, testable)."""
    return croniter(cron_expr, base).get_next(datetime)


@dataclass
class _ScopeState:
    """Pending work for one job kind: a full run, or a union of scoped names; plus
    prune."""

    pending_full: bool = False
    pending_names: set[str] = field(default_factory=set)
    pending_prune: bool = False


class TriggerCoordinator:
    """Debounces triggers into coalesced, never-overlapping runs, keyed by job kind.

    ``runners`` maps a job kind to ``runner(names, prune)`` -- ``names`` a list of
    collection names or ``None`` for a full run, ``prune`` whether to run the global
    maintenance pass. Work coalesces *within* a kind (a full run supersedes scoped
    names; scoped names union; prune is OR'd) but never *across* kinds -- distinct kinds
    fire sequentially under one run-lock (they mutate overlapping Jellyfin + state). The
    debounce window resets on each accepted trigger; an optional ``max_wait_seconds``
    caps the total delay.
    """

    def __init__(
        self,
        runners: dict[str, Callable[[list[str] | None, bool], None]],
        *,
        debounce_seconds: float,
        max_wait_seconds: float | None,
        allow_full_run: bool,
    ) -> None:
        self._runners = runners
        self._debounce = debounce_seconds
        self._max_wait = max_wait_seconds
        self._allow_full = allow_full_run
        self._lock = threading.Lock()
        self._pending: dict[str, _ScopeState] = {}
        self._first_at: float | None = None
        self._timer: threading.Timer | None = None
        self._run_lock = threading.Lock()
        self._running = False  # a fire loop is draining runs right now

    def submit(
        self,
        kind: str,
        scope: set[str] | str,
        *,
        gated: bool = True,
        prune: bool = False,
    ) -> tuple[bool, int]:
        """Enqueue a scope for one job ``kind``. Returns ``(accepted, http_status)``.

        ``gated`` applies the ``allow_full_run`` check to a full run (inbound ``/run``);
        the internal scheduler passes ``gated=False``. ``prune`` requests the
        maintenance pass (the scheduler sets it on the default-cron run).
        """
        with self._lock:
            state = self._pending.setdefault(kind, _ScopeState())
            if scope == ALL:
                if gated and not self._allow_full:
                    return (False, 403)
                state.pending_full = True
            else:
                state.pending_names |= set(scope)
            if prune:
                state.pending_prune = True
            self._arm_locked()
        return (True, 202)

    def _arm_locked(self) -> None:
        if self._running:
            # A run is in progress; don't stack a timer (under load that spawns one
            # blocked thread per trigger). The in-flight fire loop re-checks pending
            # when it finishes.
            return
        now = time.monotonic()
        if self._first_at is None:
            self._first_at = now
        delay = self._debounce
        if self._max_wait is not None:
            delay = min(delay, max(0.0, self._max_wait - (now - self._first_at)))
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(delay, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
            if self._running:
                return  # another fire loop owns the runs; it will drain what we queued
            if not self._pending:
                self._first_at = None
                return
            self._running = True
        try:
            # Drain in a loop: triggers that land mid-run accumulate in _pending and are
            # picked up by the next iteration (coalesced), rather than each arming its
            # own timer and stacking a blocked thread behind the run lock.
            while True:
                with self._lock:
                    if not self._pending:
                        self._first_at = None
                        break
                    pending = self._pending
                    self._pending = {}
                # never two runs at once -- they mutate Jellyfin + state
                with self._run_lock:
                    self._run_batch(pending)
        finally:
            with self._lock:
                self._running = False
                if self._pending:  # a trigger raced in after the last drain check
                    self._arm_locked()

    def _run_batch(self, pending: dict[str, _ScopeState]) -> None:
        for kind in sorted(pending):  # stable order; kinds run sequentially
            state = pending[kind]
            names = None if state.pending_full else sorted(state.pending_names)
            prune = state.pending_prune or state.pending_full
            if names is not None and not names and not prune:
                continue  # nothing pending for this kind
            runner = self._runners.get(kind)
            if runner is None:
                log.error("no runner registered for job kind %r", kind)
                continue
            try:
                runner(names, prune)
            except Exception as exc:  # a run failure must not kill the daemon
                log.error("%s run failed: %s", kind, exc)

    def flush(self) -> None:
        """Fire any pending work immediately (used by tests / shutdown)."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        self._fire()


@dataclass
class _Context:
    secret: str
    collection_names: set[str]
    tag_to_collection: dict[str, str]
    coordinator: TriggerCoordinator


class _Handler(BaseHTTPRequestHandler):
    server_version = "Nalanda"

    @property
    def _ctx(self) -> _Context:
        return self.server._ctx  # type: ignore[attr-defined]

    def log_message(
        self, format: str, *args: Any
    ) -> None:  # silence default stderr logging
        pass

    def _send(self, status: int, payload: dict[str, Any] | None = None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        ctx = self._ctx
        src = self.client_address[0]
        if self.path not in ("/trigger", "/run"):
            self._send(404, {"error": "not found"})
            return
        # No secret configured -> the POST routes are disabled (not merely
        # unauthenticated). This must precede the token check: hmac.compare_digest("",
        # "") is True, so an empty secret would otherwise let a tokenless request
        # through.
        if not ctx.secret:
            log.warning(
                "%s %s -> 503 (webhook disabled: no WEBHOOK_SECRET set) from %s",
                self.command,
                self.path,
                src,
            )
            self._send(503, {"error": "webhook disabled"})
            return
        if not hmac.compare_digest(self.headers.get(TOKEN_HEADER, ""), ctx.secret):
            log.warning(
                "%s %s -> 401 (bad token) from %s", self.command, self.path, src
            )
            self._send(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, {"error": "bad content-length"})
            return
        if length > MAX_BODY:
            self._send(413, {"error": "body too large"})
            return
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw) if raw else {}
            if not isinstance(body, dict):
                raise ValueError
        except ValueError, json.JSONDecodeError:
            self._send(400, {"error": "invalid json"})
            return

        if self.path == "/run":
            accepted, status = ctx.coordinator.submit("collections", ALL, gated=True)
            log.info(
                "POST /run from %s -> %d (%s)",
                src,
                status,
                "queued" if accepted else "refused",
            )
            self._send(
                status,
                {"status": "accepted" if accepted else "forbidden", "scope": "all"},
            )
            return

        names = resolve_scope(
            body,
            collection_names=ctx.collection_names,
            tag_to_collection=ctx.tag_to_collection,
        )
        if not names:
            log.info("POST /trigger from %s -> 204 (no matching scope)", src)
            self._send(204, None)
            return
        accepted, status = ctx.coordinator.submit("collections", names, gated=True)
        log.info("POST /trigger from %s -> %d (scope %s)", src, status, sorted(names))
        self._send(status, {"status": "accepted", "scope": sorted(names)})


def _scheduler_loop(
    cron_expr: str,
    kind: str,
    scope: set[str] | str,
    prune: bool,
    coordinator: TriggerCoordinator,
    stop: threading.Event,
) -> None:
    label = "FULL" if scope == ALL else (sorted(scope) or "prune-only")
    while not stop.is_set():
        base = datetime.now()
        wait = (next_cron(cron_expr, base) - base).total_seconds()
        if stop.wait(timeout=max(1.0, wait)):
            return
        log.info("scheduled %s run %s (cron %r)", kind, label, cron_expr)
        coordinator.submit(kind, scope, gated=False, prune=prune)


def serve(secrets: Secrets, *, dry_run: bool = False) -> int:
    """Run the webhook daemon until interrupted."""
    if not Path(secrets.nalanda_config).exists():
        log.error(
            "No config file at %s. A first `run`/`serve` seeds a starter there; "
            "edit it (and .env) and restart.",
            secrets.nalanda_config,
        )
        return 1

    # WEBHOOK_SECRET is optional: without it the daemon still runs on schedules and
    # serves /health, but the authenticated POST routes are disabled (see
    # _Handler.do_POST). The startup banner below reports which mode this is.
    cfg = load_config(secrets.nalanda_config)
    wh = cfg.settings.webhook

    # Bind address/port are deployment config (env / .env; see Secrets), not config.yml.
    # The default host is 127.0.0.1 (loopback) so a bare `serve` is not exposed; a
    # container sets NALANDA_HOST=0.0.0.0 to publish it, and should sit behind a
    # TLS-terminating reverse proxy -- the token travels in clear over HTTP.
    host = secrets.nalanda_host
    port = secrets.nalanda_port

    # Resolve the schedule cascade (already validated at config load) into
    # {cron: collections} groups plus the default cron whose run also prunes. One
    # scheduler thread per distinct cron fires below; per-collection schedules run
    # scoped, the default cron runs + prunes.
    schedule_groups, default_cron = cfg.resolve_schedules("collections")
    if default_cron is not None:
        # The default cron carries pruning even if every collection overrides it --
        # ensure it has a run (an empty scope reconciles nothing but still sweeps
        # orphans + folders).
        schedule_groups.setdefault(default_cron, set())

    # Reverse map: identity-tag label -> collection name, for Radarr/Sonarr-managed
    # collections (so a Radarr or Sonarr webhook carrying nalanda- tags scopes the run
    # to those collections). Only enabled collections are tagged in Radarr/Sonarr, so
    # only they can be scoped by a tag.
    tag_to_collection: dict[str, str] = {}
    for name, coll in cfg.collections.items():
        if coll.radarr is not None and coll.radarr.enable:
            label = identity_tag_label(name, effective_radarr(coll, cfg.settings))
            tag_to_collection[label.casefold()] = name
        if coll.sonarr is not None and coll.sonarr.enable:
            label = sonarr_identity_tag_label(
                name, effective_sonarr(coll, cfg.settings)
            )
            tag_to_collection[label.casefold()] = name

    # Late import to avoid a circular dependency (__main__ imports serve).
    from .__main__ import _run, _run_metadata

    def runner(names: list[str] | None, prune: bool) -> None:
        log.info(
            "run start: %s%s%s",
            "FULL" if names is None else f"scoped {names}",
            " +prune" if prune else "",
            " (dry-run)" if dry_run else "",
        )
        _run(secrets, names=names, dry_run=dry_run, prune=prune)

    def metadata_runner(names: list[str] | None, prune: bool) -> None:
        # Metadata is a single monolithic job; names/prune don't apply to it.
        log.info("metadata run start%s", " (dry-run)" if dry_run else "")
        _run_metadata(secrets, dry_run=dry_run)

    metadata_cron = cfg.resolve_job_cron("metadata")

    coordinator = TriggerCoordinator(
        {"collections": runner, "metadata": metadata_runner},
        debounce_seconds=wh.debounce_seconds,
        max_wait_seconds=wh.max_wait_seconds,
        allow_full_run=wh.allow_full_run,
    )
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd._ctx = _Context(  # type: ignore[attr-defined]
        secret=secrets.webhook_secret,
        collection_names=set(cfg.collections),
        tag_to_collection=tag_to_collection,
        coordinator=coordinator,
    )

    stop = threading.Event()
    for cron_expr, scope_names in schedule_groups.items():
        threading.Thread(
            target=_scheduler_loop,
            args=(
                cron_expr,
                "collections",
                set(scope_names),
                cron_expr == default_cron,
                coordinator,
                stop,
            ),
            daemon=True,
        ).start()
    if metadata_cron is not None:
        threading.Thread(
            target=_scheduler_loop,
            args=(metadata_cron, "metadata", ALL, False, coordinator, stop),
            daemon=True,
        ).start()

    scheduled = {n for members in schedule_groups.values() for n in members}
    unscheduled = sorted(set(cfg.collections) - scheduled)
    log.info(
        "Nalanda serve on %s:%d | POST /trigger, POST /run, GET /health "
        "| debounce %ss | full-run %s | %d schedule(s) "
        "| %d managed collection(s)%s",
        host,
        port,
        wh.debounce_seconds,
        "allowed" if wh.allow_full_run else "blocked",
        len(schedule_groups),
        len(tag_to_collection),
        " | DRY-RUN" if dry_run else "",
    )
    if secrets.webhook_secret:
        log.info("  webhook POST routes enabled (X-Nalanda-Token required)")
    else:
        log.warning(
            "  webhook POST routes DISABLED (no WEBHOOK_SECRET set); serving /health + "
            "schedules only. Set WEBHOOK_SECRET to enable POST /trigger and /run."
        )
    for cron_expr, scope_names in sorted(schedule_groups.items()):
        members = sorted(scope_names)
        desc = ", ".join(members) if members else "(prune only)"
        prune_note = " +prune" if cron_expr == default_cron else ""
        log.info("  schedule %r%s -> %s", cron_expr, prune_note, desc)
    if metadata_cron is not None:
        log.info("  metadata schedule %r", metadata_cron)
    else:
        log.info("  metadata: no schedule (run on demand via `nalanda run metadata`)")
    if schedule_groups:
        if unscheduled:
            log.warning(
                "%d collection(s) have no schedule (webhook / manual run only): %s",
                len(unscheduled),
                unscheduled,
            )
        if default_cron is None:
            log.warning(
                "no global or per-type 'collections' schedule set; orphan + "
                "empty-folder pruning runs only on a manual `run` or POST /run, "
                "never on a per-collection schedule"
            )
    elif cfg.collections:
        log.info(
            "no schedules configured; running purely on webhook triggers and "
            "manual runs"
        )
    if not secrets.webhook_secret and not schedule_groups:
        log.warning(
            "Daemon has no schedules and no webhook secret -- it will idle. "
            "Define a schedule or set WEBHOOK_SECRET (after filling in %s and its "
            ".env), then restart.",
            secrets.nalanda_config,
        )

    # Friendly check: the state file's directory must be writable, or runs can't
    # persist their idempotency markers. Warn (don't exit -- exiting would
    # restart-loop under a restart policy).
    state_dir = Path(secrets.nalanda_state).parent
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if not os.access(state_dir, os.W_OK):
        uid = getattr(os, "getuid", lambda: "?")()
        log.warning(
            "State dir %s is not writable (uid=%s); runs cannot persist their markers. "
            "Make the mounted directory writable by the container's user.",
            state_dir,
            uid,
        )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        stop.set()
        httpd.shutdown()
    return 0
