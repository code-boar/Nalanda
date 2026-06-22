"""Unit tests for artwork resolution, the local repo, and the apply dispatch
(no network)."""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from nalanda.artwork import (
    collection_dir,
    ensure_folders,
    prune_folders,
    resolve_artwork,
)
from nalanda.collection import _apply
from nalanda.config import ArtworkRepo, CollectionDef, Config
from nalanda.models import ImageSource
from nalanda.reconcile import CollectionPlan

# A 1x1 PNG -- enough that the bytes (and so the hash) are real.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000154a24f8b0000000049454e44ae426082"
)
_TMDB = {
    "Primary": "http://tmdb/p.jpg",
    "Thumb": "http://tmdb/t.jpg",
    "Backdrop": "http://tmdb/b.jpg",
}


def _coll(**kw) -> CollectionDef:
    return CollectionDef(media="movie", **kw)


# --- resolution precedence ------------------------------------------------


def test_falls_back_to_tmdb_when_nothing_set():
    out = resolve_artwork("C", _coll(), None, tmdb_images=_TMDB)
    assert out["Primary"] == ImageSource.from_url("http://tmdb/p.jpg")
    assert out["Primary"].marker == "http://tmdb/p.jpg"


def test_none_when_no_source_at_all():
    out = resolve_artwork(
        "C",
        _coll(),
        None,
        tmdb_images={"Primary": None, "Thumb": None, "Backdrop": None},
    )
    assert out == {"Primary": None, "Thumb": None, "Backdrop": None}


def test_explicit_url_wins_over_tmdb():
    coll = _coll(primary_art_url="http://cfg/poster.png")
    out = resolve_artwork("C", coll, None, tmdb_images=_TMDB)
    assert out["Primary"] == ImageSource.from_url("http://cfg/poster.png")


def test_explicit_file_wins_and_carries_hash_and_mime(tmp_path):
    f = tmp_path / "poster.png"
    f.write_bytes(_PNG)
    out = resolve_artwork("C", _coll(primary_art_file=str(f)), None, tmdb_images=_TMDB)
    src = out["Primary"]
    assert src.kind == "file"
    assert src.ref == str(f)
    assert src.marker.startswith("file:")
    assert src.content_type == "image/png"


def test_bad_url_warns_and_falls_through(caplog):
    coll = _coll(primary_art_url="not-a-url")
    with caplog.at_level(logging.WARNING):
        out = resolve_artwork("C", coll, None, tmdb_images=_TMDB)
    assert out["Primary"] == ImageSource.from_url("http://tmdb/p.jpg")  # fell through
    assert "is not a valid URL" in caplog.text


def test_missing_file_warns_and_falls_through(caplog, tmp_path):
    coll = _coll(primary_art_file=str(tmp_path / "nope.png"))
    with caplog.at_level(logging.WARNING):
        out = resolve_artwork("C", coll, None, tmdb_images=_TMDB)
    assert out["Primary"] == ImageSource.from_url("http://tmdb/p.jpg")
    assert "not found" in caplog.text


# --- the repo -------------------------------------------------------------


def _repo(tmp_path, **kw) -> ArtworkRepo:
    return ArtworkRepo(path=str(tmp_path), **kw)


def test_repo_file_used_when_no_explicit(tmp_path):
    folder = tmp_path / "collections" / "my-coll"
    folder.mkdir(parents=True)
    (folder / "primary.png").write_bytes(_PNG)
    out = resolve_artwork("My Coll", _coll(), _repo(tmp_path), tmdb_images=_TMDB)
    assert out["Primary"].kind == "file"
    assert out["Primary"].ref == str(folder / "primary.png")
    # other slots still fall through to TMDB
    assert out["Thumb"] == ImageSource.from_url("http://tmdb/t.jpg")


def test_explicit_file_beats_repo(tmp_path):
    folder = tmp_path / "collections" / "my-coll"
    folder.mkdir(parents=True)
    (folder / "primary.png").write_bytes(_PNG)
    explicit = tmp_path / "explicit.jpg"
    explicit.write_bytes(_PNG)
    out = resolve_artwork(
        "My Coll",
        _coll(primary_art_file=str(explicit)),
        _repo(tmp_path),
        tmdb_images=_TMDB,
    )
    assert out["Primary"].ref == str(explicit)


def test_repo_extension_precedence_png_over_jpg(tmp_path, caplog):
    folder = tmp_path / "collections" / "c"
    folder.mkdir(parents=True)
    (folder / "primary.png").write_bytes(_PNG)
    (folder / "primary.jpg").write_bytes(_PNG)
    with caplog.at_level(logging.WARNING):
        out = resolve_artwork("C", _coll(), _repo(tmp_path), tmdb_images=_TMDB)
    assert out["Primary"].ref.endswith("primary.png")
    assert "multiple files" in caplog.text


def test_file_marker_changes_with_bytes(tmp_path):
    f = tmp_path / "poster.png"
    f.write_bytes(_PNG)
    first = resolve_artwork(
        "C", _coll(primary_art_file=str(f)), None, tmdb_images=_TMDB
    )
    f.write_bytes(_PNG + b"\x00extra")
    second = resolve_artwork(
        "C", _coll(primary_art_file=str(f)), None, tmdb_images=_TMDB
    )
    assert first["Primary"].marker != second["Primary"].marker


def test_collection_dir_uses_slug(tmp_path):
    assert (
        collection_dir(_repo(tmp_path), "A Title: Subtitle").name == "a-title-subtitle"
    )


# --- folder maintenance ---------------------------------------------------


def test_ensure_folders_creates_per_collection(tmp_path):
    repo = _repo(tmp_path, create_empty_folders=True)
    ensure_folders(repo, ["My Coll", "Other"], dry_run=False)
    assert (tmp_path / "collections" / "my-coll").is_dir()
    assert (tmp_path / "collections" / "other").is_dir()


def test_ensure_folders_dry_run_creates_nothing(tmp_path):
    repo = _repo(tmp_path, create_empty_folders=True)
    ensure_folders(repo, ["My Coll"], dry_run=True)
    assert not (tmp_path / "collections").exists()


def test_ensure_folders_noop_without_flag(tmp_path):
    ensure_folders(_repo(tmp_path), ["C"], dry_run=False)
    assert not (tmp_path / "collections").exists()


def test_prune_removes_empty_unconfigured_only(tmp_path):
    base = tmp_path / "collections"
    (base / "gone").mkdir(parents=True)  # empty + unconfigured -> remove
    keep_cfg = base / "kept"  # empty but still configured -> keep
    keep_cfg.mkdir()
    nonempty = base / "stale-but-has-files"  # unconfigured but non-empty -> keep
    nonempty.mkdir()
    (nonempty / "primary.png").write_bytes(_PNG)

    repo = _repo(tmp_path, delete_old_empty_folders=True)
    prune_folders(repo, ["Kept"], dry_run=False)

    assert not (base / "gone").exists()
    assert keep_cfg.is_dir()
    assert nonempty.is_dir()


def test_prune_dry_run_removes_nothing(tmp_path):
    base = tmp_path / "collections"
    (base / "gone").mkdir(parents=True)
    repo = _repo(tmp_path, delete_old_empty_folders=True)
    prune_folders(repo, [], dry_run=True)
    assert (base / "gone").is_dir()


def test_prune_noop_without_flag(tmp_path):
    base = tmp_path / "collections"
    (base / "gone").mkdir(parents=True)
    prune_folders(_repo(tmp_path), [], dry_run=False)
    assert (base / "gone").is_dir()


# --- config validation ----------------------------------------------------


def test_url_and_file_mutually_exclusive():
    with pytest.raises(ValidationError, match="set only one of"):
        _coll(primary_art_url="http://x/p.jpg", primary_art_file="/p.png")


def test_duplicate_slugs_rejected_with_repo(tmp_path):
    with pytest.raises(ValidationError, match="slugify"):
        Config(
            artwork_repo=ArtworkRepo(path=str(tmp_path)),
            collections={
                "A Title: Subtitle": CollectionDef(media="movie"),
                "A Title Subtitle": CollectionDef(media="movie"),
            },
        )


def test_duplicate_slugs_allowed_without_repo():
    # No artwork repo -> no folder collision to guard against.
    cfg = Config(
        collections={
            "A Title: Subtitle": CollectionDef(media="movie"),
            "A Title Subtitle": CollectionDef(media="movie"),
        }
    )
    assert len(cfg.collections) == 2


# --- apply dispatch (download vs. upload) ---------------------------------


class _FakeJF:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def delete_image(self, cid, *, image_type="Primary"):
        self.calls.append(("delete", image_type))

    def set_remote_image(self, cid, url, *, image_type="Primary"):
        self.calls.append(("remote", image_type, url))

    def upload_image(self, cid, data, *, content_type, image_type="Primary"):
        self.calls.append(("upload", image_type, content_type, data))


def test_apply_dispatches_url_and_file(tmp_path):
    f = tmp_path / "bd.png"
    f.write_bytes(_PNG)
    plan = CollectionPlan(name="C")
    plan.set_images = {"Primary": "http://x/p.jpg", "Backdrop": "file:abc"}
    sources = {
        "Primary": ImageSource.from_url("http://x/p.jpg"),
        "Backdrop": ImageSource(
            kind="file", ref=str(f), marker="file:abc", content_type="image/png"
        ),
    }
    jf = _FakeJF()
    _apply(jf, plan, sources, existing_id="cid")

    assert ("remote", "Primary", "http://x/p.jpg") in jf.calls
    # Backdrop is cleared first (Jellyfin appends), then uploaded with bytes + mime.
    assert jf.calls.index(("delete", "Backdrop")) < next(
        i for i, c in enumerate(jf.calls) if c[0] == "upload"
    )
    upload = next(c for c in jf.calls if c[0] == "upload")
    assert upload[1] == "Backdrop"
    assert upload[2] == "image/png"
    assert upload[3] == _PNG
