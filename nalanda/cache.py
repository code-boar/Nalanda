"""Best-effort, disposable metadata cache over a single SQLite file.

The cache exists to de-duplicate calls to the external metadata providers (TMDB, TVDB,
MDBList) across runs and across collections within a run. It is a pure performance/
rate-limit aid: every row can be rebuilt from the sources, which shapes the whole
design.

Two invariants:

* **Cache source facts, never reconciliation state.** Only data that is a pure function
  of an id and rarely changes lives here. Jellyfin and Radarr/Sonarr *library* state
  (what we diff against and write to) is never cached -- that stays the clients' job.
* **A broken cache is a cache miss.** Any failure -- a corrupt file, a locked database,
  a decode error -- degrades to a miss (reads) or a no-op (writes), logged at debug,
  never propagated. Mirrors :func:`nalanda.state.load_state`, which returns ``{}`` on
  any error.

Expiry is **lazy**: there is no background deletion. A row's freshness is computed at
read time (``now - created_at <= ttl``); a stale row simply sits until the next fetch of
that key overwrites it, or ``nalanda cache prune`` sweeps it.

Storage is one key-value table (``cache``) plus a ``meta`` table holding a per-namespace
schema version. Namespaces (``tmdb.record``, ``tmdb.query``, ``radarr.lookup``, ...)
group
keys; a version bump purges only its own namespace. This is the only module besides
``state.py`` that persists to disk, and -- like ``state.py`` -- it owns its own SQLite
file separate from the authoritative state file (opposite durability contracts).
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple, TypeVar

from .logging import get_logger

log = get_logger(__name__)

T = TypeVar("T")

# A fresh write back-dates created_at by up to this fraction of its TTL, so a cold start
# (which fills the whole cache in one pass) doesn't make every entry expire in the same
# window and trigger a synchronized mass re-fetch on a later run.
JITTER_FRACTION = 0.15

# Fixed, non-user-tunable TTLs (seconds) for the Radarr/Sonarr add-lookup caches. These
# are upstream-protection / operational knobs, not source-freshness preferences, so they
# are not exposed in `settings.cache` (see docs/design/caching.md).
LOOKUP_TTL = 30 * 86400
LOOKUP_EMPTY_TTL = 1 * 86400
ADD_FAILED_TTL = 6 * 3600

# Public source namespaces -> the CacheSettings duration field that governs each. The
# four user-facing knobs each cover a group of namespaces; the taxonomy stays internal.
NAMESPACE_BUCKET: dict[str, str] = {
    "tmdb.record": "record_cache_duration",
    "tmdb.resolve": "record_cache_duration",
    "tvdb.record": "record_cache_duration",
    "tmdb.list": "list_cache_duration",
    "tvdb.list": "list_cache_duration",
    "mdblist.list": "list_cache_duration",
    "tmdb.query": "query_cache_duration",
    "tvdb.query": "query_cache_duration",
    "tmdb.chart": "chart_cache_duration",
}

# Arr namespaces -> their fixed internal TTL.
_ARR_TTLS: dict[str, float] = {
    "radarr.lookup": LOOKUP_TTL,
    "sonarr.lookup": LOOKUP_TTL,
    "radarr.lookup_empty": LOOKUP_EMPTY_TTL,
    "sonarr.lookup_empty": LOOKUP_EMPTY_TTL,
    "radarr.add_failed": ADD_FAILED_TTL,
    "sonarr.add_failed": ADD_FAILED_TTL,
}

# Schema version per namespace. Bump when that namespace's stored payload SHAPE changes;
# on a mismatch the cache purges only that namespace (the data is reconstructible).
# CAVEAT: the record/resolve/list/query/lookup namespaces all serialize MediaItem /
# MovieCollection, so a change to those models is a payload change for ALL of them --
# bump every affected entry together.
_VERSIONS: dict[str, int] = {ns: 1 for ns in (*NAMESPACE_BUCKET, *_ARR_TTLS)}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    namespace  TEXT NOT NULL,
    key        TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (namespace, key)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class _Row(NamedTuple):
    payload: str
    created_at: float


def parse_duration(value: str | int | float) -> float:
    """A cache-duration string -> seconds. ``<int>d`` / ``<int>h``; ``0``/``off`` ->
    ``0.0``.

    ``0.0`` means "do not cache this bucket" (a bypass). Minutes -- and anything else --
    are rejected: the finest sensible granularity for a metadata cache is an hour, and a
    minute-scale TTL would defeat the cache and hammer the providers for no real
    freshness.
    """
    s = str(value).strip().lower()
    if s in ("0", "off", ""):
        return 0.0
    unit, digits = s[-1:], s[:-1]
    if unit in ("d", "h") and digits.isdigit():
        return int(digits) * (86400.0 if unit == "d" else 3600.0)
    raise ValueError(
        f"cache duration {value!r}: use <int>d or <int>h (e.g. 30d, 6h), or 0/off to "
        "disable; minutes are not allowed"
    )


def ttl_map(cache_settings: Any) -> dict[str, float]:
    """Resolve a ``CacheSettings`` into a ``{namespace: ttl_seconds}`` map.

    Source namespaces read their bucket's duration field; the Arr namespaces use the
    fixed internal constants. The argument is typed ``Any`` so this module stays free of
    a
    ``config`` import (``config`` imports from here, not the other way around).
    """
    ttls = {
        ns: parse_duration(getattr(cache_settings, field))
        for ns, field in NAMESPACE_BUCKET.items()
    }
    ttls.update(_ARR_TTLS)
    return ttls


class Cache:
    """A best-effort SQLite cache. One instance per run, used from that run's single
    thread."""

    def __init__(
        self,
        path: str | Path,
        *,
        ttls: dict[str, float] | None = None,
        refresh: bool = False,
    ) -> None:
        self.path = str(path)
        self._ttls = dict(ttls or {})
        self._refresh = refresh  # force every read to miss (the --refresh flag)
        self._hit = 0
        self._expired = 0
        self._miss = 0
        self._versioned = False

    # --- connection + schema + versioning --------------------------------------------

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        try:
            conn.executescript(
                _SCHEMA
            )  # first statement to touch the file -> raises if corrupt
        except sqlite3.DatabaseError:
            conn.close()  # release handle so a corrupt file can be unlinked (Windows)
            raise
        return conn

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = self._open()
        except sqlite3.DatabaseError:
            # Corrupt file -> discard and recreate once; the cache is reconstructible.
            Path(self.path).unlink(missing_ok=True)
            conn = self._open()
        if not self._versioned:
            self._check_versions(conn)
            self._versioned = True
        return conn

    def _check_versions(self, conn: sqlite3.Connection) -> None:
        stored = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        changed = False
        for ns, version in _VERSIONS.items():
            if stored.get(f"version:{ns}") != str(version):
                conn.execute("DELETE FROM cache WHERE namespace = ?", (ns,))
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (f"version:{ns}", str(version)),
                )
                changed = True
        if changed:
            conn.commit()

    # --- low-level row access (each opens its own connection) ------------------------

    def _get(self, namespace: str, key: str) -> _Row | None:
        try:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT payload, created_at FROM cache "
                    "WHERE namespace = ? AND key = ?",
                    (namespace, key),
                ).fetchone()
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            log.debug("cache read failed (%s:%s): %s", namespace, key, exc)
            return None
        return _Row(row[0], row[1]) if row else None

    def _put(self, namespace: str, key: str, payload: str, ttl: float) -> None:
        created = time.time() - random.uniform(0, JITTER_FRACTION) * ttl
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO cache(namespace, key, payload, created_at) "
                    "VALUES(?,?,?,?) "
                    "ON CONFLICT(namespace, key) DO UPDATE SET "
                    "payload = excluded.payload, created_at = excluded.created_at",
                    (namespace, key, payload, created),
                )
                conn.commit()
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            log.debug("cache write failed (%s:%s): %s", namespace, key, exc)

    # --- the main read-through helper ------------------------------------------------

    def _ttl(self, namespace: str) -> float:
        return self._ttls.get(namespace, 0.0)

    def fetch(
        self,
        namespace: str,
        key: str,
        *,
        ttl: float,
        loader: Callable[[], T],
        dump: Callable[[T], str] = json.dumps,
        load: Callable[[str], T] = json.loads,
        refresh: bool = False,
    ) -> T:
        """Return a cached value or compute it via ``loader`` and store it.

        ``ttl`` is in seconds; ``0`` bypasses the cache entirely (always loads, never
        stores). A fresh row is a hit; a present-but-stale row counts *expired*, an
        absent key *miss* -- both then call ``loader``. ``loader`` exceptions propagate
        (real API errors must surface); cache-internal errors degrade to a miss.
        """
        stale_row_existed = False
        if ttl and not (self._refresh or refresh):
            row = self._get(namespace, key)
            if row is not None:
                if (time.time() - row.created_at) <= ttl:
                    try:
                        value = load(row.payload)
                        self._hit += 1
                        return value
                    except Exception as exc:  # corrupt/incompatible payload -> refetch
                        log.debug(
                            "cache decode failed (%s:%s): %s", namespace, key, exc
                        )
                else:
                    stale_row_existed = True
        if stale_row_existed:
            self._expired += 1
        else:
            self._miss += 1
        value = loader()
        if ttl:
            try:
                payload = dump(value)
            except Exception as exc:
                log.debug("cache encode failed (%s:%s): %s", namespace, key, exc)
            else:
                self._put(namespace, key, payload, ttl)
        return value

    # --- raw accessors for bespoke flows (the Arr negative cache) --------------------

    def read(self, namespace: str, key: str) -> str | None:
        """The payload of a FRESH row (TTL-checked against the namespace's ttl), else
        ``None``.

        Does not touch the hit/miss counters -- it backs the two-namespace Arr lookup
        cache, whose control flow doesn't fit :meth:`fetch`'s single-key shape.
        """
        ttl = self._ttl(namespace)
        if not ttl or self._refresh:
            return None
        row = self._get(namespace, key)
        if row is not None and (time.time() - row.created_at) <= ttl:
            return row.payload
        return None

    def write(self, namespace: str, key: str, payload: str) -> None:
        """Store a payload under the namespace's configured TTL (no-op if ttl is 0)."""
        ttl = self._ttl(namespace)
        if ttl:
            self._put(namespace, key, payload, ttl)

    # --- maintenance (the `nalanda cache` subcommand) --------------------------------

    def purge(self, namespace: str) -> None:
        """Delete every row in a namespace."""
        try:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM cache WHERE namespace = ?", (namespace,))
                conn.commit()
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            log.debug("cache purge failed (%s): %s", namespace, exc)

    def namespaces(self) -> dict[str, int]:
        """A ``{namespace: row count}`` map of what's currently stored."""
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT namespace, COUNT(*) FROM cache GROUP BY namespace"
                ).fetchall()
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            log.debug("cache namespaces failed: %s", exc)
            return {}
        return {ns: count for ns, count in rows}

    def prune_expired(self) -> int:
        """Delete every row whose TTL has elapsed (per its namespace). Returns the count
        removed."""
        removed = 0
        now = time.time()
        try:
            conn = self._connect()
            try:
                for ns, ttl in self._ttls.items():
                    if ttl <= 0:
                        continue
                    cur = conn.execute(
                        "DELETE FROM cache "
                        "WHERE namespace = ? AND (? - created_at) > ?",
                        (ns, now, ttl),
                    )
                    removed += cur.rowcount
                conn.commit()
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            log.debug("cache prune failed: %s", exc)
        return removed

    @property
    def summary(self) -> str:
        return f"{self._hit} hit, {self._expired} expired, {self._miss} miss"

    def close(self) -> None:
        """No persistent connection to close (connection-per-operation); kept for
        symmetry."""
