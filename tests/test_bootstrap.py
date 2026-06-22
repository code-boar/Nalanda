"""Tests for first-run config/.env scaffolding (nalanda.bootstrap)."""

from __future__ import annotations

from nalanda.bootstrap import ensure_config_scaffold, refresh_config_schema
from nalanda.config import load_config


def test_ensure_scaffold_seeds_when_absent(tmp_path):
    cfg = tmp_path / "config.yml"
    created = ensure_config_scaffold(cfg)
    assert created is True
    assert cfg.exists()
    assert (tmp_path / ".env").exists()
    assert load_config(cfg).collections == {}  # the seeded starter validates


def test_ensure_scaffold_never_overwrites(tmp_path):
    cfg = tmp_path / "config.yml"
    cfg.write_text("collections: {}\n", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("TMDB_API_KEY=sentinel\n", encoding="utf-8")

    created = ensure_config_scaffold(cfg)
    assert created is False  # an existing config is left untouched
    assert env.read_text(encoding="utf-8") == "TMDB_API_KEY=sentinel\n"


def test_seeded_env_is_owner_only(tmp_path):
    # the seeded .env will hold secrets, so it should be created owner-only (POSIX)
    import os
    import stat

    import pytest

    if os.name != "posix":
        pytest.skip("file mode bits are POSIX-specific")
    ensure_config_scaffold(tmp_path / "config.yml")
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600


def test_refresh_writes_schema_next_to_config(tmp_path):
    import json

    cfg = tmp_path / "config.yml"
    wrote = refresh_config_schema(cfg)
    assert wrote is True
    schema_file = tmp_path / "config.schema.json"
    assert schema_file.exists()  # written beside config.yml, where the editor looks
    doc = json.loads(schema_file.read_text(encoding="utf-8"))
    assert doc["title"] == "Nalanda configuration"
    assert "collections" in doc["properties"]


def test_refresh_overwrites_stale_schema(tmp_path):
    import json

    # A derived artifact, not user data: a startup refresh replaces whatever is there
    # (unlike the starter config/.env, which are only seeded when absent).
    schema_file = tmp_path / "config.schema.json"
    schema_file.write_text("{ stale }", encoding="utf-8")
    refresh_config_schema(tmp_path / "config.yml")
    assert json.loads(schema_file.read_text(encoding="utf-8"))["title"] == (
        "Nalanda configuration"
    )


def test_refresh_unwritable_does_not_raise(tmp_path, monkeypatch):
    # The schema is an authoring convenience: a read-only/permission-denied config dir
    # must not crash the daemon (unlike a missing config, which exits). Just skip it.
    def boom(self, *a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("pathlib.Path.write_text", boom)
    assert refresh_config_schema(tmp_path / "config.yml") is False


def test_ensure_scaffold_unwritable_exits_cleanly(tmp_path, monkeypatch):
    # An unwritable config dir (e.g. a root-owned bind mount) must exit cleanly with an
    # actionable message, not crash with a traceback.
    import pytest

    def boom(self, *a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("pathlib.Path.write_text", boom)
    with pytest.raises(SystemExit) as exc_info:
        ensure_config_scaffold(tmp_path / "config.yml")
    assert exc_info.value.code == 1
