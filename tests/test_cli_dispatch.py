"""CLI dispatch tests for `nalanda run` (job selection, flags, isolation)."""

from __future__ import annotations

import nalanda.__main__ as main_mod
import nalanda.bootstrap as bootstrap_mod


def test_run_forwards_refresh_cache(monkeypatch, tmp_path):
    # Point at a config path that does not exist and no-op the scaffold, so main()
    # skips config loading and seeding -- we only want to observe dispatch.
    monkeypatch.setenv("NALANDA_CONFIG", str(tmp_path / "config.yml"))
    monkeypatch.setattr(bootstrap_mod, "ensure_config_scaffold", lambda *a, **k: None)

    captured: dict[str, object] = {}

    def fake_run(
        secrets, names=None, *, dry_run=False, refresh_cache=False, prune=None
    ):
        captured["names"] = names
        captured["dry_run"] = dry_run
        captured["refresh_cache"] = refresh_cache
        return 0

    monkeypatch.setattr(main_mod, "_run", fake_run)
    monkeypatch.setattr(main_mod, "_run_metadata", lambda *a, **k: 0)

    assert main_mod.main(["run", "--refresh-cache"]) == 0
    assert captured["refresh_cache"] is True

    captured.clear()
    assert main_mod.main(["run"]) == 0
    assert captured["refresh_cache"] is False


def test_version_flag_prints_version(capsys):
    import nalanda

    assert main_mod.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == nalanda.__version__


def test_help_flag_prints_usage(capsys):
    assert main_mod.main(["--help"]) == 0
    assert "Usage:" in capsys.readouterr().out


def _patch_jobs(monkeypatch, *, run_rc=0, md_rc=0, run_exc=None):
    """Replace the two job runners with recorders; return the calls list."""
    calls: list[tuple] = []

    def fake_run(
        secrets, names=None, *, dry_run=False, refresh_cache=False, prune=None
    ):
        calls.append(("collections", names, dry_run, refresh_cache))
        if run_exc is not None:
            raise run_exc
        return run_rc

    def fake_metadata(secrets, *, dry_run=False):
        calls.append(("metadata", dry_run))
        return md_rc

    monkeypatch.setattr(main_mod, "_run", fake_run)
    monkeypatch.setattr(main_mod, "_run_metadata", fake_metadata)
    return calls


SECRETS = object()  # the fakes ignore it


def test_dispatch_global_runs_both_in_order(monkeypatch):
    calls = _patch_jobs(monkeypatch)
    assert main_mod._dispatch_run(SECRETS, [], dry_run=False, refresh_cache=False) == 0
    assert calls == [("collections", None, False, False), ("metadata", False)]


def test_dispatch_collections_only(monkeypatch):
    calls = _patch_jobs(monkeypatch)
    assert main_mod._dispatch_run(SECRETS, ["collections"]) == 0
    assert calls == [("collections", None, False, False)]


def test_dispatch_collections_scoped(monkeypatch):
    calls = _patch_jobs(monkeypatch)
    assert main_mod._dispatch_run(SECRETS, ["collections", "A", "B"]) == 0
    assert calls == [("collections", ["A", "B"], False, False)]


def test_dispatch_metadata_only(monkeypatch):
    calls = _patch_jobs(monkeypatch)
    assert main_mod._dispatch_run(SECRETS, ["metadata"]) == 0
    assert calls == [("metadata", False)]


def test_dispatch_unknown_target_errors(monkeypatch):
    calls = _patch_jobs(monkeypatch)
    assert main_mod._dispatch_run(SECRETS, ["bogus"]) == 1
    assert calls == []  # nothing ran


def test_dispatch_metadata_rejects_names(monkeypatch):
    calls = _patch_jobs(monkeypatch)
    assert main_mod._dispatch_run(SECRETS, ["metadata", "A"]) == 1
    assert calls == []


def test_dispatch_isolates_failure_and_aggregates_exit_code(monkeypatch):
    calls = _patch_jobs(monkeypatch, run_rc=1)
    # collections fails (rc=1) but metadata still runs; overall exit code is 1.
    assert main_mod._dispatch_run(SECRETS, []) == 1
    assert [c[0] for c in calls] == ["collections", "metadata"]


def test_dispatch_isolates_exception(monkeypatch):
    calls = _patch_jobs(monkeypatch, run_exc=RuntimeError("boom"))
    assert main_mod._dispatch_run(SECRETS, []) == 1
    assert [c[0] for c in calls] == ["collections", "metadata"]


def test_dispatch_propagates_dry_run(monkeypatch):
    calls = _patch_jobs(monkeypatch)
    assert main_mod._dispatch_run(SECRETS, [], dry_run=True) == 0
    assert calls == [("collections", None, True, False), ("metadata", True)]
