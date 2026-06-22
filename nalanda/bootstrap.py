"""First-run scaffolding.

When `run`/`serve` starts with no config file, seed a minimal starter ``config.yml``
and a sibling ``.env`` from bundled templates, so a fresh deployment (a new Docker
``/config`` volume, a clean checkout) comes up with files to edit instead of erroring
on a missing config. Existing files are never overwritten.
"""

from __future__ import annotations

import json
import os
from importlib.resources import files
from pathlib import Path

from .logging import get_logger

log = get_logger(__name__)


def ensure_config_scaffold(config_path: str | Path) -> bool:
    """Seed a starter ``config.yml`` (and a sibling ``.env``) when the config is absent.

    Returns ``True`` if the starter config was created, ``False`` if it already existed.
    Never overwrites an existing file. The templates ship inside the package
    (``nalanda/templates``), so they are always available -- locally and in the image.

    If the config directory isn't writable (a common Docker first-run mistake: a
    bind-mounted ``/config`` left owned by root), this logs an actionable message and
    exits cleanly rather than crashing with a traceback.
    """
    cfg = Path(config_path)
    if cfg.exists():
        return False
    templates = files("nalanda.templates")
    env = cfg.parent / ".env"
    seeded_env = False
    try:
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(
            templates.joinpath("config.starter.yml").read_text("utf-8"),
            encoding="utf-8",
        )
        if not env.exists():
            env.write_text(
                templates.joinpath("env.starter").read_text("utf-8"), encoding="utf-8"
            )
            try:
                env.chmod(0o600)  # .env holds secrets once filled in -> owner-only
            except OSError:
                pass  # best effort; some platforms/filesystems ignore chmod
            seeded_env = True
    except OSError as exc:
        uid = getattr(os, "getuid", lambda: "?")()
        gid = getattr(os, "getgid", lambda: "?")()
        log.error(
            "Cannot write to config directory %s (running as %s:%s): %s."
            " Make it writable by that user -- on the host,"
            " `chown %s:%s %s` (or set the container's `user:` to the"
            " directory's owner), then restart.",
            cfg.parent,
            uid,
            gid,
            exc,
            uid,
            gid,
            cfg.parent,
        )
        raise SystemExit(1) from None
    log.warning(
        "First run: created starter %s%s. Add your Jellyfin URL and API key, define "
        "collections, then restart.",
        cfg,
        f" and {env}" if seeded_env else "",
    )
    return True


def refresh_config_schema(config_path: str | Path) -> bool:
    """Write the JSON Schema next to ``config.yml`` so the editor can validate it.

    The schema referenced by the starter config (``$schema=./config.schema.json``) is a
    derived artifact, generated here from the same models the image runs, so it always
    matches this version -- no remote URL to drift ahead of a pinned deployment. Unlike
    the starter config/.env, it is rewritten on every startup (it is generated, not
    user-edited), so an upgrade refreshes it on the next restart.

    Returns ``True`` if written. A write failure (e.g. a read-only config dir) is logged
    and swallowed -- an authoring convenience is never a reason to stop the daemon.
    """
    from .config import json_schema

    out = Path(config_path).parent / "config.schema.json"
    try:
        out.write_text(json.dumps(json_schema(), indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("Could not write editor schema to %s: %s (skipping).", out, exc)
        return False
    log.debug("Refreshed editor schema at %s", out)
    return True
