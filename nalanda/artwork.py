"""Resolve a collection's artwork and maintain the local artwork repository.

Each Jellyfin image slot (Primary / Thumb / Backdrop) is resolved in priority order:

1. an explicit per-slot ``*_art_file`` (local bytes) or ``*_art_url`` (remote URL);
2. a file in the artwork repo at ``<root>/collections/<slug>/<type>.<ext>``;
3. the auto-sourced TMDB image from the builders;
4. nothing.

A missing/invalid *explicit* value is a warning that falls through to the next tier; an
absent repo file is normal and silent. Local files carry a content-hash marker, so
editing a file's bytes (under a stable path) re-uploads while an otherwise unchanged run
is a no-op. The slug is the same one used for identity tags, so a collection has one
identity on disk.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

from .config import ArtworkRepo, CollectionDef
from .logging import get_logger
from .models import ImageSource
from .tagging import slugify

log = get_logger(__name__)

# (config slot, Jellyfin image type). The repo filename token is the lowercase config
# slot.
_SLOTS: tuple[tuple[str, str], ...] = (
    ("primary", "Primary"),
    ("thumb", "Thumb"),
    ("backdrop", "Backdrop"),
)
# Accepted extensions in precedence order: PNG (lossless) > JPEG > WebP. When several
# exist for one slot the first wins and the rest are ignored (with a warning).
_EXTS: tuple[str, ...] = ("png", "jpg", "jpeg", "webp")
_MIME: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}


def _looks_like_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _file_source(path: Path) -> ImageSource:
    """An :class:`ImageSource` for a local file: bytes hashed for the idempotency
    marker."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    mime = _MIME.get(path.suffix.lstrip(".").lower(), "image/jpeg")
    return ImageSource(
        kind="file", ref=str(path), marker=f"file:{digest}", content_type=mime
    )


def collection_dir(repo: ArtworkRepo, name: str) -> Path:
    """The artwork folder for a collection: ``<root>/collections/<slug>/``."""
    return Path(repo.path or "").expanduser() / "collections" / slugify(name)


def _repo_file(repo: ArtworkRepo, name: str, slot: str) -> Path | None:
    """The repo image for one slot, honouring extension precedence
    (png > jpg/jpeg > webp).

    Warns and ignores all but the winner when more than one candidate file is present.
    """
    folder = collection_dir(repo, name)
    found = [
        folder / f"{slot}.{ext}"
        for ext in _EXTS
        if (folder / f"{slot}.{ext}").is_file()
    ]
    if not found:
        return None
    if len(found) > 1:
        log.warning(
            "  %-32s artwork %s: multiple files %s; using %s",
            name,
            slot,
            [p.name for p in found],
            found[0].name,
        )
    return found[0]


def _resolve_slot(
    name: str,
    coll: CollectionDef,
    repo: ArtworkRepo | None,
    slot: str,
    jf_type: str,
    tmdb_images: dict[str, str | None],
) -> ImageSource | None:
    """Resolve a single slot through the precedence chain (see module docstring)."""
    file_val = getattr(coll, f"{slot}_art_file")
    url_val = getattr(coll, f"{slot}_art_url")
    # 1) explicit override (url xor file, enforced by config). A bad value warns + falls
    # through.
    if file_val:
        p = Path(file_val).expanduser()
        if p.is_file():
            return _file_source(p)
        log.warning(
            "  %-32s %s_art_file %r not found -- falling back to artwork repo,"
            " then TMDB",
            name,
            slot,
            file_val,
        )
    elif url_val:
        if _looks_like_url(url_val):
            return ImageSource.from_url(url_val)
        log.warning(
            "  %-32s %s_art_url %r is not a valid URL -- falling back to artwork"
            " repo, then TMDB",
            name,
            slot,
            url_val,
        )
    # 2) artwork repo (always checked when no usable explicit override). A miss is
    # silent.
    if repo and repo.path:
        repo_path = _repo_file(repo, name, slot)
        if repo_path is not None:
            return _file_source(repo_path)
    # 3) auto-sourced TMDB image.
    tmdb_url = tmdb_images.get(jf_type)
    if tmdb_url:
        return ImageSource.from_url(tmdb_url)
    # 4) nothing.
    return None


def resolve_artwork(
    name: str,
    coll: CollectionDef,
    repo: ArtworkRepo | None,
    *,
    tmdb_images: dict[str, str | None],
) -> dict[str, ImageSource | None]:
    """Resolve all three Jellyfin slots for a collection to image sources
    (or ``None``)."""
    return {
        jf_type: _resolve_slot(name, coll, repo, slot, jf_type, tmdb_images)
        for slot, jf_type in _SLOTS
    }


def ensure_folders(
    repo: ArtworkRepo | None, names: Iterable[str], *, dry_run: bool
) -> None:
    """Create an empty ``<root>/collections/<slug>/`` for each configured collection."""
    if not (repo and repo.path and repo.create_empty_folders):
        return
    for name in names:
        folder = collection_dir(repo, name)
        if folder.is_dir():
            continue
        if dry_run:
            log.info("  artwork: [dry-run] would create %s", folder)
        else:
            folder.mkdir(parents=True, exist_ok=True)
            log.info("  artwork: created %s", folder)


def prune_folders(
    repo: ArtworkRepo | None, names: Iterable[str], *, dry_run: bool
) -> None:
    """Delete *empty* ``<root>/collections/<slug>/`` folders no longer configured.

    Only ever removes an empty directory directly under ``collections/`` whose slug is
    not in the configured set; any folder that still contains files is always left
    untouched.
    """
    if not (repo and repo.path and repo.delete_old_empty_folders):
        return
    base = Path(repo.path).expanduser() / "collections"
    if not base.is_dir():
        return
    keep = {slugify(n) for n in names}
    for child in sorted(base.iterdir()):
        if not child.is_dir() or child.name in keep:
            continue
        if any(child.iterdir()):  # non-empty -> never touch
            continue
        if dry_run:
            log.info("  artwork: [dry-run] would remove empty %s", child)
        else:
            child.rmdir()
            log.info("  artwork: removed empty %s", child)
