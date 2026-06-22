"""Shared identity-tag naming for the Radarr and Sonarr sync engines.

Both engines own collection identity tags of the form ``<tag_prefix><slug-or-override>``
and derive the slug the same way; that shared logic lives here so the two stay in
lockstep.
"""

from __future__ import annotations

import re


def slugify(name: str) -> str:
    """A tag-friendly slug of a collection name (lowercase, hyphenated)."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    return slug or "collection"


def build_identity_tag(name: str, *, tag_prefix: str, tag: str | None) -> str:
    """A collection's identity tag label: ``<tag_prefix><slug-or-override>``."""
    return f"{tag_prefix}{tag or slugify(name)}"
