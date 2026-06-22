"""Shared HTTP layer.

A thin wrapper around a configured :class:`niquests.Session`. Every API client
(TMDB, Radarr, Jellyfin, ...) subclasses :class:`BaseClient`, so the rest of the
codebase never imports ``niquests`` directly and the HTTP backend can be swapped
in this one file.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Self

import niquests

from .logging import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRIES = 3
# Status codes worth retrying: transient server errors + rate limiting. (niquests' own
# `retries` only covers transport/connection errors, not error *responses*.)
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 0.5  # seconds; doubles each attempt
RETRY_AFTER_CAP = 60.0  # max seconds to honor from a 429's Retry-After header
# Methods safe to replay after an ambiguous 5xx (a write may have been applied
# server-side before the error). A bare POST is excluded so a transient 5xx can't
# double-create. 429 is handled separately -- it means the request was NOT processed,
# so it replays for any method.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS"})


class RateLimiter:
    """Token-bucket limiter: a sustained ``rate`` req/s with a ``burst`` bucket.

    Thread-safe. :meth:`acquire` blocks (``time.sleep``) until a token is free. In
    Nalanda's serial-per-run execution this behaves as a minimum-interval spacer with a
    burst allowance; the lock is cheap defensiveness should the ``serve`` daemon ever
    run requests on threads.
    """

    def __init__(self, rate: float, burst: float | None = None) -> None:
        self._rate = float(rate)
        self._capacity = float(burst if burst is not None else rate)
        self._tokens = self._capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def set_rate(self, rate: float, burst: float | None = None) -> None:
        """Change the sustained rate (and burst); clamp tokens to the new cap."""
        with self._lock:
            self._rate = float(rate)
            self._capacity = float(burst if burst is not None else rate)
            # tokens never go below 0, so this keeps 0 <= tokens <= capacity
            self._tokens = min(self._tokens, self._capacity)

    def acquire(self) -> None:
        """Block until one token is available, then consume it."""
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._last) * self._rate
                )
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1.0 - self._tokens) / self._rate
            time.sleep(wait)  # sleep outside the lock


class HTTPError(Exception):
    """Raised when a request fails to send or returns a non-OK status."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        url: str | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.url = url
        self.body = body


def _safe_body(resp: niquests.Response, limit: int = 500) -> str:
    try:
        return (resp.text or "")[:limit]
    except Exception:  # pragma: no cover - defensive
        return ""


def _should_retry(method: str, status: int | None) -> bool:
    """Whether a non-OK response should be retried.

    A 429 (rate limit) means the request was never processed -> safe to replay any
    method. A 5xx is ambiguous (a write may have succeeded server-side before the
    error), so it is only replayed for idempotent methods -- never a bare POST, which
    could double-create.
    """
    if status == 429:
        return True
    return status in RETRY_STATUSES and method.upper() in IDEMPOTENT_METHODS


def _retry_after(resp: niquests.Response, attempt: int) -> float:
    """Wait time for a 429: the ``Retry-After`` header (integer seconds, capped at
    :data:`RETRY_AFTER_CAP`) when present, else exponential backoff.

    A non-integer (e.g. HTTP-date) or absent header falls back to exponential backoff;
    the three sources send integer seconds.
    """
    header = getattr(resp, "headers", None) or {}
    value = header.get("Retry-After")
    if value:
        try:
            secs = int(value)
        except TypeError, ValueError:
            secs = 0  # not integer seconds -> fall through to exponential backoff
        if secs > 0:
            if secs > RETRY_AFTER_CAP:
                log.warning(
                    "Retry-After %ss exceeds cap; waiting %.0fs instead",
                    secs,
                    RETRY_AFTER_CAP,
                )
                return RETRY_AFTER_CAP
            return float(secs)
    return RETRY_BACKOFF * (2**attempt)


class BaseClient:
    """Base for API clients: base URL, default params/headers, retries, JSON helper."""

    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        multiplexed: bool = False,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._default_params: dict[str, Any] = dict(params or {})
        self._limiter = limiter
        self._session = niquests.Session(
            base_url=base_url,
            timeout=timeout,
            retries=retries,
            multiplexed=multiplexed,
        )
        self._session.headers.update({"Accept": "application/json"})
        if headers:
            # .items() yields an Iterable[tuple[str, str]]; HTTPHeaderDict.update no
            # longer accepts a plain dict[str, str] under its stricter stubs.
            self._session.headers.update(headers.items())

    @property
    def session(self) -> niquests.Session:
        return self._session

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        not_found_ok: bool = False,
    ) -> Any:
        """Send a request and return decoded JSON.

        Returns ``None`` for empty bodies, and for 404s when ``not_found_ok``.
        Raises :class:`HTTPError` on transport failure or non-OK status.
        """
        merged = {**self._default_params, **(params or {})}
        for attempt in range(RETRY_ATTEMPTS):
            if self._limiter is not None:
                self._limiter.acquire()
            try:
                resp = self._session.request(
                    method, path, params=merged or None, json=json
                )
            except niquests.exceptions.RequestException as exc:
                # A transport error (read timeout, dropped connection) means no response
                # came back. An idempotent method has no side effects, so retry it with
                # backoff; a bare POST may have been applied server-side, so it stays
                # fatal (no double-apply).
                if (
                    method.upper() in IDEMPOTENT_METHODS
                    and attempt < RETRY_ATTEMPTS - 1
                ):
                    wait = RETRY_BACKOFF * (2**attempt)
                    log.warning(
                        "%s %s -> %s; retrying in %.1fs (attempt %d/%d)",
                        method,
                        path,
                        type(exc).__name__,
                        wait,
                        attempt + 1,
                        RETRY_ATTEMPTS,
                    )
                    time.sleep(wait)
                    continue
                raise HTTPError(f"{method} {path} failed: {exc}", url=path) from exc

            if not_found_ok and resp.status_code == 404:
                return None
            if resp.ok:
                return resp.json() if resp.content else None

            # retry rate limits (any method) + transient 5xx (idempotent methods only)
            if _should_retry(method, resp.status_code) and attempt < RETRY_ATTEMPTS - 1:
                wait = (
                    _retry_after(resp, attempt)
                    if resp.status_code == 429
                    else RETRY_BACKOFF * (2**attempt)
                )
                log.warning(
                    "%s %s -> HTTP %d; retrying in %.1fs (attempt %d/%d)",
                    method,
                    path,
                    resp.status_code,
                    wait,
                    attempt + 1,
                    RETRY_ATTEMPTS,
                )
                time.sleep(wait)
                continue
            raise HTTPError(
                f"{method} {path} -> HTTP {resp.status_code}",
                status=resp.status_code,
                url=str(resp.url),
                body=_safe_body(resp),
            )

    def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        not_found_ok: bool = False,
    ) -> Any:
        return self.request_json("GET", path, params=params, not_found_ok=not_found_ok)

    def post(
        self, path: str, *, params: dict[str, Any] | None = None, json: Any = None
    ) -> Any:
        return self.request_json("POST", path, params=params, json=json)

    def put(
        self, path: str, *, params: dict[str, Any] | None = None, json: Any = None
    ) -> Any:
        return self.request_json("PUT", path, params=params, json=json)

    def delete(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return self.request_json("DELETE", path, params=params)

    def post_bytes(
        self,
        path: str,
        data: bytes,
        *,
        content_type: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        """POST a raw byte body with an explicit Content-Type (e.g. an image upload).

        Unlike :meth:`request_json` this sends bytes, not JSON, and ignores the (empty)
        response. Retries follow the same policy -- a bare POST is replayed only on a
        429, never on an ambiguous 5xx -- so the upload can't be double-applied.
        """
        merged = {**self._default_params, **(params or {})}
        headers = {"Content-Type": content_type}
        for attempt in range(RETRY_ATTEMPTS):
            if self._limiter is not None:
                self._limiter.acquire()
            try:
                resp = self._session.request(
                    "POST", path, params=merged or None, data=data, headers=headers
                )
            except niquests.exceptions.RequestException as exc:
                raise HTTPError(f"POST {path} failed: {exc}", url=path) from exc

            if resp.ok:
                return
            if _should_retry("POST", resp.status_code) and attempt < RETRY_ATTEMPTS - 1:
                wait = (
                    _retry_after(resp, attempt)
                    if resp.status_code == 429
                    else RETRY_BACKOFF * (2**attempt)
                )
                log.warning(
                    "POST %s -> HTTP %d; retrying in %.1fs (attempt %d/%d)",
                    path,
                    resp.status_code,
                    wait,
                    attempt + 1,
                    RETRY_ATTEMPTS,
                )
                time.sleep(wait)
                continue
            raise HTTPError(
                f"POST {path} -> HTTP {resp.status_code}",
                status=resp.status_code,
                url=str(resp.url),
                body=_safe_body(resp),
            )

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
