"""Tiny local state, persisted next to ``config.yml``.

Some things we apply to Jellyfin can't be read back to check idempotency -- most
notably a collection's **images**: Jellyfin only exposes a post-download image hash,
not the source URL. So we remember what we last applied here and compare on the next
run, re-pulling only when the URL actually changes.

Shape: ``{"<collection name>": {"Primary": url, "Thumb": url, "Backdrop": url}}``
(any slot may be null).
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from .logging import get_logger

log = get_logger(__name__)

# Fallback when a caller doesn't supply a path. The real path derives from the config
# directory (config.Secrets.nalanda_state) and is passed in by the CLI -- a Docker
# deployment mounts that directory as a volume so the state file survives restarts.
DEFAULT_STATE_PATH = ".nalanda-state.json"

# The cross-process lock lives next to the state file (same dir/volume). Coordinates a
# manual `run` with the `serve` daemon's runs -- both mutate Jellyfin, Radarr and the
# state file.
DEFAULT_LOCK_PATH = f"{DEFAULT_STATE_PATH}.lock"


@contextmanager
def run_lock(
    *,
    path: str = DEFAULT_LOCK_PATH,
    wait: bool = True,
    poll: float = 30.0,
    notify: Callable[[str], None] | None = None,
) -> Iterator[None]:
    """Hold a cross-process lock for the duration of a run.

    A manual ``run`` and the daemon are separate processes, so an in-process lock can't
    coordinate them. This file lock does: whoever runs first holds it; a second run
    finds it held and, with ``wait`` (the default), blocks until it clears -- printing a
    one-line notice -- then proceeds. With ``wait=False`` a :class:`filelock.Timeout`
    is raised instead. Dry-runs skip this entirely (they write nothing). The OS releases
    the lock if a holder crashes, so it can't go stale.
    """
    say = notify or log.info
    lock = FileLock(path)
    try:
        lock.acquire(timeout=0.0)
    except Timeout:
        if not wait:
            raise
        say(
            "Another Nalanda job is in progress; this run is queued and will start "
            "once it finishes."
        )
        while True:
            try:
                lock.acquire(timeout=poll)
                break
            except Timeout:
                say("...still waiting for the current job to finish.")
    try:
        yield
    finally:
        lock.release()


def load_state(path: str | Path = DEFAULT_STATE_PATH) -> dict[str, Any]:
    """Read the state file.

    Returns ``{}`` when the file is missing (a normal first run) or holds invalid JSON
    (corruption we can't use anyway -- rebuilding is the only way forward). But an
    existing file that can't be *read* (an I/O or permission error) raises rather than
    returning ``{}``: the state file is not reconstructible, so silently treating a
    readable-but-erroring file as empty would rebuild and overwrite the authoritative
    record. Fail loud.
    """
    p = Path(path)
    if not p.exists():
        return {}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"State file {p} exists but could not be read ({exc}); it is not "
            "reconstructible. Refusing to continue and overwrite it -- inspect or "
            "remove it, then re-run."
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: str | Path, data: dict[str, Any]) -> None:
    """Write the state file atomically (pretty-printed, UTF-8).

    Writes a temp file in the same directory, then ``os.replace`` onto the target -- an
    atomic rename on Windows and POSIX -- so a crash mid-write can never truncate the
    authoritative state file (which, unlike the cache, is not reconstructible).
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
