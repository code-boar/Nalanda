"""Unit tests for per-item metadata config + pure planner (no network)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from nalanda import __main__ as main_mod
from nalanda.config import Config, MetadataEntry, Secrets
from nalanda.metadata import FIELD_MAP, plan_item_metadata, plan_metadata_cleanup
from nalanda.models import JellyfinItem, JellyfinLibrary
from nalanda.sorting import expected_sort_name


def test_metadata_entry_requires_exactly_one_id():
    with pytest.raises(ValidationError):
        MetadataEntry(parental_rating="GB-15")  # no id
    with pytest.raises(ValidationError):
        MetadataEntry(tmdb=1, imdb="tt1", parental_rating="GB-15")  # two ids
    with pytest.raises(ValidationError):
        MetadataEntry(tmdb=1, imdb="tt1", tvdb=2, parental_rating="GB-15")  # three ids


def test_metadata_entry_requires_a_field():
    with pytest.raises(ValidationError):
        MetadataEntry(tmdb=1)  # id but no fields


def test_metadata_entry_provider_key_and_fields():
    e = MetadataEntry(tmdb=1234, parental_rating="GB-15", sort_title="A Film 2")
    assert e.provider_key == "tmdb:1234"
    assert e.field_values() == {"parental_rating": "GB-15", "sort_title": "A Film 2"}


def test_metadata_entry_provider_key_tvdb_and_imdb():
    assert MetadataEntry(tvdb=5678, title="X").provider_key == "tvdb:5678"
    assert MetadataEntry(imdb="tt0090605", title="X").provider_key == "imdb:tt0090605"


def test_metadata_entry_coerces_numeric_values_to_str():
    # YAML parses bare numbers (UK ratings like 15, numeric titles like 2000) as ints;
    # Jellyfin stores these fields as strings, so a number is accepted and stringified.
    e = MetadataEntry(tmdb=1234, parental_rating=15, title=2000, sort_title=2)
    assert e.parental_rating == "15"
    assert e.title == "2000"
    assert e.sort_title == "2"
    assert e.field_values() == {
        "parental_rating": "15",
        "title": "2000",
        "sort_title": "2",
    }


def test_config_parses_metadata_block():
    cfg = Config.model_validate(
        {
            "metadata": {
                "movies": [
                    {"tmdb": 1234, "parental_rating": "GB-15", "sort_title": "A Film 2"}
                ],
                "shows": [{"tvdb": 5678, "parental_rating": "GB-12"}],
            }
        }
    )
    assert cfg.metadata.movies[0].provider_key == "tmdb:1234"
    assert cfg.metadata.shows[0].tvdb == 5678


def test_config_rejects_unknown_metadata_key():
    with pytest.raises(ValidationError):
        Config.model_validate({"metadata": {"movies": [{"tmdb": 1, "bogus": "x"}]}})


def test_resolve_job_cron_uses_jobs_override():
    cfg = Config.model_validate(
        {"settings": {"run_schedule": "0 4 * * *", "jobs": {"metadata": "0 6 * * *"}}}
    )
    assert cfg.resolve_job_cron("metadata") == "0 6 * * *"


def test_resolve_job_cron_falls_back_to_global():
    cfg = Config.model_validate({"settings": {"run_schedule": "0 4 * * *"}})
    assert cfg.resolve_job_cron("metadata") == "0 4 * * *"


def test_resolve_job_cron_off_sentinel_disables():
    cfg = Config.model_validate(
        {"settings": {"run_schedule": "0 4 * * *", "jobs": {"metadata": "none"}}}
    )
    assert cfg.resolve_job_cron("metadata") is None


def test_resolve_job_cron_named_schedule():
    cfg = Config.model_validate(
        {
            "settings": {
                "run_schedules": {"daily": "0 4 * * *"},
                "jobs": {"metadata": "daily"},
            }
        }
    )
    assert cfg.resolve_job_cron("metadata") == "0 4 * * *"


def test_jobs_metadata_invalid_cron_rejected():
    with pytest.raises(ValidationError):
        Config.model_validate({"settings": {"jobs": {"metadata": "not a cron"}}})


def test_resolve_job_cron_unknown_kind_raises():
    with pytest.raises(ValueError):
        Config().resolve_job_cron("bogus_kind")


def test_unlock_unconfigured_metadata_default_false():
    assert Config().settings.unlock_unconfigured_metadata is False


def test_metadata_state_path_is_config_sibling():
    s = Secrets(_env_file=None, nalanda_config="/cfg/config.yml")
    assert s.nalanda_metadata_state.replace("\\", "/").endswith(
        "/cfg/.nalanda-metadata-state.json"
    )


def test_empty_desired_yields_up_to_date_plan():
    p = plan_item_metadata(
        {}, {"OfficialRating": "GB-15", "LockedFields": ["OfficialRating"]}
    )
    assert p.up_to_date is True
    assert p.changes == {}


def test_field_map_shape():
    assert FIELD_MAP["parental_rating"] == ("OfficialRating", "OfficialRating")
    assert FIELD_MAP["title"] == ("Name", "Name")
    assert FIELD_MAP["overview"] == ("Overview", "Overview")
    assert FIELD_MAP["sort_title"] == ("ForcedSortName", None)


def test_writes_value_and_lock_when_absent():
    p = plan_item_metadata(
        {"parental_rating": "GB-15"}, {"OfficialRating": None, "LockedFields": []}
    )
    assert p.changes["OfficialRating"] == "GB-15"
    assert p.changes["LockedFields"] == ["OfficialRating"]
    assert p.up_to_date is False


def test_idempotent_when_value_and_lock_present():
    p = plan_item_metadata(
        {"parental_rating": "GB-15"},
        {"OfficialRating": "GB-15", "LockedFields": ["OfficialRating"]},
    )
    assert p.changes == {}
    assert p.up_to_date is True


def test_value_matches_but_lock_missing_rewrites():
    p = plan_item_metadata({"overview": "x"}, {"Overview": "x", "LockedFields": []})
    assert p.changes["Overview"] == "x"
    assert p.changes["LockedFields"] == ["Overview"]


def test_lock_union_preserves_existing_locks():
    p = plan_item_metadata({"title": "T"}, {"Name": "old", "LockedFields": ["Genres"]})
    assert sorted(p.changes["LockedFields"]) == ["Genres", "Name"]


def test_sort_title_idempotent_via_expected_sort_name():
    dto = {"SortName": expected_sort_name("A Film 2"), "LockedFields": []}
    p = plan_item_metadata({"sort_title": "A Film 2"}, dto)
    assert p.up_to_date is True


def test_sort_title_written_when_mismatched():
    p = plan_item_metadata(
        {"sort_title": "A Film 2"}, {"SortName": "old name", "LockedFields": []}
    )
    assert p.changes["ForcedSortName"] == "A Film 2"


def test_forced_sort_name_reasserted_on_other_write():
    # sort_title itself is settled, but a parental_rating write must re-assert
    # ForcedSortName because Jellyfin's GET omits it and the RMW would otherwise
    # null it.
    dto = {
        "SortName": expected_sort_name("A Film 2"),
        "OfficialRating": None,
        "LockedFields": [],
    }
    p = plan_item_metadata({"parental_rating": "GB-15", "sort_title": "A Film 2"}, dto)
    assert p.changes["OfficialRating"] == "GB-15"
    assert p.changes["ForcedSortName"] == "A Film 2"


def test_cleanup_drops_lock_and_clears_sort():
    changes = plan_metadata_cleanup(
        ["parental_rating", "sort_title"], {"LockedFields": ["OfficialRating", "Name"]}
    )
    assert changes["LockedFields"] == ["Name"]
    assert changes["ForcedSortName"] == ""


def test_cleanup_noop_when_nothing_to_unlock():
    assert plan_metadata_cleanup(["parental_rating"], {"LockedFields": []}) == {}


def test_cleanup_only_sort_title_clears_forced_sort_name():
    changes = plan_metadata_cleanup(
        ["sort_title"], {"LockedFields": ["OfficialRating"]}
    )
    assert changes == {"ForcedSortName": ""}


class _FakeJF:
    """Minimal Jellyfin stand-in: canned library items + captured update_item calls."""

    def __init__(self, url, key):
        self.items: dict[str, dict] = {}
        self.updates: list[tuple[str, dict]] = []
        self._movies: list[JellyfinItem] = []
        self._series: list[JellyfinItem] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def resolve_libraries(self, *, collection_type=None):
        if collection_type == "movies":
            return [
                JellyfinLibrary(id="movlib", name="Movies", collection_type="movies")
            ]
        if collection_type == "tvshows":
            return [
                JellyfinLibrary(id="tvlib", name="Shows", collection_type="tvshows")
            ]
        return []

    def get_movies(self, library_ids=None):
        return self._movies

    def get_series(self, library_ids=None):
        return self._series

    def get_item(self, item_id):
        return dict(self.items[item_id])  # a copy, like the real read-modify-write read

    def update_item(self, item_id, changes):
        self.updates.append((item_id, changes))
        self.items[item_id].update(changes)


def _secrets(tmp_path):
    return Secrets(
        _env_file=None,
        nalanda_config=str(tmp_path / "config.yml"),
        jellyfin_url="http://jf",
        jellyfin_api_key="k",
    )


def test_run_metadata_writes_and_locks(tmp_path, monkeypatch):
    (tmp_path / "config.yml").write_text(
        "metadata:\n  movies:\n    - tmdb: 1234\n      parental_rating: GB-15\n",
        encoding="utf-8",
    )
    jf = _FakeJF("u", "k")
    jf._movies = [JellyfinItem(id="m1", name="A Film", provider_ids={"Tmdb": "1234"})]
    jf.items["m1"] = {"OfficialRating": None, "LockedFields": []}
    monkeypatch.setattr(main_mod, "JellyfinClient", lambda url, key: jf)

    rc = main_mod._run_metadata(_secrets(tmp_path), dry_run=False)

    assert rc == 0
    assert jf.updates[0][0] == "m1"
    assert jf.updates[0][1]["OfficialRating"] == "GB-15"
    assert jf.updates[0][1]["LockedFields"] == ["OfficialRating"]
    state = json.loads((tmp_path / ".nalanda-metadata-state.json").read_text())
    assert state["tmdb:1234"] == {"jellyfin_id": "m1", "fields": ["parental_rating"]}


def test_run_metadata_dry_run_writes_nothing(tmp_path, monkeypatch):
    (tmp_path / "config.yml").write_text(
        "metadata:\n  movies:\n    - tmdb: 1234\n      parental_rating: GB-15\n",
        encoding="utf-8",
    )
    jf = _FakeJF("u", "k")
    jf._movies = [JellyfinItem(id="m1", name="A Film", provider_ids={"Tmdb": "1234"})]
    jf.items["m1"] = {"OfficialRating": None, "LockedFields": []}
    monkeypatch.setattr(main_mod, "JellyfinClient", lambda url, key: jf)

    rc = main_mod._run_metadata(_secrets(tmp_path), dry_run=True)

    assert rc == 0
    assert jf.updates == []
    assert not (tmp_path / ".nalanda-metadata-state.json").exists()


def test_run_metadata_idempotent_second_run(tmp_path, monkeypatch):
    (tmp_path / "config.yml").write_text(
        "metadata:\n  movies:\n    - tmdb: 1234\n      parental_rating: GB-15\n",
        encoding="utf-8",
    )
    jf = _FakeJF("u", "k")
    jf._movies = [JellyfinItem(id="m1", name="A Film", provider_ids={"Tmdb": "1234"})]
    jf.items["m1"] = {"OfficialRating": None, "LockedFields": []}
    monkeypatch.setattr(main_mod, "JellyfinClient", lambda url, key: jf)

    main_mod._run_metadata(_secrets(tmp_path), dry_run=False)
    jf.updates.clear()
    main_mod._run_metadata(_secrets(tmp_path), dry_run=False)

    assert jf.updates == []  # already locked + valued -> no second write


def test_run_metadata_cleanup_unlocks_orphan(tmp_path, monkeypatch):
    (tmp_path / "config.yml").write_text(
        "settings:\n  unlock_unconfigured_metadata: true\nmetadata:\n  movies: []\n",
        encoding="utf-8",
    )
    (tmp_path / ".nalanda-metadata-state.json").write_text(
        json.dumps({"tmdb:1234": {"jellyfin_id": "m1", "fields": ["parental_rating"]}}),
        encoding="utf-8",
    )
    jf = _FakeJF("u", "k")
    jf.items["m1"] = {"OfficialRating": "GB-15", "LockedFields": ["OfficialRating"]}
    monkeypatch.setattr(main_mod, "JellyfinClient", lambda url, key: jf)

    rc = main_mod._run_metadata(_secrets(tmp_path), dry_run=False)

    assert rc == 0
    assert jf.updates[0][1]["LockedFields"] == []  # unlocked
    state = json.loads((tmp_path / ".nalanda-metadata-state.json").read_text())
    assert "tmdb:1234" not in state


def test_run_metadata_cleanup_skips_unmatched_declared(tmp_path, monkeypatch):
    # An item still declared in config but NOT found in the library this run must
    # not be recorded as managed: its desired fields were never written/locked, so
    # resurrecting the state record with those fields (and a stale id) would mislead
    # the next run.
    (tmp_path / "config.yml").write_text(
        "settings:\n  unlock_unconfigured_metadata: true\n"
        "metadata:\n  movies:\n    - tmdb: 1234\n      parental_rating: GB-15\n",
        encoding="utf-8",
    )
    (tmp_path / ".nalanda-metadata-state.json").write_text(
        json.dumps({"tmdb:1234": {"jellyfin_id": "m1", "fields": ["sort_title"]}}),
        encoding="utf-8",
    )
    jf = _FakeJF("u", "k")
    jf._movies = []  # item is NOT in the library this run -> won't match
    jf.items["m1"] = {"LockedFields": []}  # old id still resolves, so cleanup can act
    monkeypatch.setattr(main_mod, "JellyfinClient", lambda url, key: jf)

    rc = main_mod._run_metadata(_secrets(tmp_path), dry_run=False)

    assert rc == 0
    state = json.loads((tmp_path / ".nalanda-metadata-state.json").read_text())
    # not resurrected with the declared-but-never-written field set
    assert state.get("tmdb:1234", {}).get("fields") != ["parental_rating"]
    assert "tmdb:1234" not in state


def test_run_metadata_cleanup_disabled_retains_state(tmp_path, monkeypatch):
    (tmp_path / "config.yml").write_text("metadata:\n  movies: []\n", encoding="utf-8")
    (tmp_path / ".nalanda-metadata-state.json").write_text(
        json.dumps({"tmdb:1234": {"jellyfin_id": "m1", "fields": ["parental_rating"]}}),
        encoding="utf-8",
    )
    jf = _FakeJF("u", "k")
    jf.items["m1"] = {"OfficialRating": "GB-15", "LockedFields": ["OfficialRating"]}
    monkeypatch.setattr(main_mod, "JellyfinClient", lambda url, key: jf)

    rc = main_mod._run_metadata(_secrets(tmp_path), dry_run=False)

    assert rc == 0
    assert jf.updates == []  # cleanup off -> no unlock
    state = json.loads((tmp_path / ".nalanda-metadata-state.json").read_text())
    assert "tmdb:1234" in state  # retained so enabling cleanup later still works


def test_run_metadata_writes_show_via_tvdb(tmp_path, monkeypatch):
    (tmp_path / "config.yml").write_text(
        "metadata:\n  shows:\n    - tvdb: 5678\n      parental_rating: GB-12\n",
        encoding="utf-8",
    )
    jf = _FakeJF("u", "k")
    jf._series = [JellyfinItem(id="s1", name="Show", provider_ids={"Tvdb": "5678"})]
    jf.items["s1"] = {"OfficialRating": None, "LockedFields": []}
    monkeypatch.setattr(main_mod, "JellyfinClient", lambda url, key: jf)

    rc = main_mod._run_metadata(_secrets(tmp_path), dry_run=False)

    assert rc == 0
    assert jf.updates[0][0] == "s1"
    assert jf.updates[0][1]["OfficialRating"] == "GB-12"
    assert jf.updates[0][1]["LockedFields"] == ["OfficialRating"]
    state = json.loads((tmp_path / ".nalanda-metadata-state.json").read_text())
    assert state["tvdb:5678"] == {"jellyfin_id": "s1", "fields": ["parental_rating"]}
