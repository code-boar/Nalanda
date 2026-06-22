"""Offline unit tests for the Jellyfin client models and matching (no network)."""

from __future__ import annotations

import pytest

from nalanda.clients.jellyfin import JellyfinClient
from nalanda.matching import LibraryIndex, MediaRoutedIndex
from nalanda.models import JellyfinCollection, JellyfinItem, JellyfinLibrary, MediaItem


def test_client_requires_creds():
    with pytest.raises(ValueError):
        JellyfinClient("", "key")
    with pytest.raises(ValueError):
        JellyfinClient("http://host", "")


def test_client_sets_authorization_header():
    client = JellyfinClient("http://host:8096", "SECRET")
    assert client.session.headers.get("Authorization") == 'MediaBrowser Token="SECRET"'


def test_update_item_strips_trickplay(monkeypatch):
    # Jellyfin serializes `Trickplay` on GET but 500s deserializing it back on
    # POST /Items/{id}, so update_item must drop it from the echoed DTO.
    client = JellyfinClient("http://host:8096", "SECRET")
    monkeypatch.setattr(
        client,
        "get_item",
        lambda item_id: {
            "Id": item_id,
            "Name": "Example",
            "OfficialRating": "PG",
            "Trickplay": {"640": {"0": {"Width": 640}}},
        },
    )
    sent: dict = {}

    def fake_post(path, json=None, **kwargs):
        sent["path"] = path
        sent["json"] = json

    monkeypatch.setattr(client, "post", fake_post)
    client.update_item("itemid", {"OfficialRating": "15"})
    assert sent["path"] == "Items/itemid"
    assert "Trickplay" not in sent["json"]  # the offending field is gone
    assert sent["json"]["OfficialRating"] == "15"  # the change is still applied


def test_library_from_jellyfin():
    lib = JellyfinLibrary.from_jellyfin(
        {"Name": "Movies", "CollectionType": "movies", "ItemId": "abc123"}
    )
    assert lib.id == "abc123"
    assert lib.collection_type == "movies"


def test_collection_from_jellyfin():
    coll = JellyfinCollection.from_jellyfin(
        {"Id": "abc", "Name": "My Coll", "ChildCount": 3, "DisplayOrder": "Default"}
    )
    assert coll.id == "abc"
    assert coll.name == "My Coll"
    assert coll.child_count == 3
    assert coll.display_order == "Default"


def test_item_provider_ids_coerced_to_str():
    item = JellyfinItem.from_jellyfin(
        {
            "Id": "jf1",
            "Name": "Example Film One",
            "ProductionYear": 2003,
            "Type": "Movie",
            "ProviderIds": {"Tmdb": 9011, "Imdb": "tt9000005", "Tvdb": None},
        }
    )
    assert item.provider_ids["Tmdb"] == "9011"  # int coerced to str
    assert item.provider_ids["Imdb"] == "tt9000005"
    assert "Tvdb" not in item.provider_ids  # None dropped


def _item(jid, tmdb=None, imdb=None, tvdb=None):
    pids = {}
    if tmdb is not None:
        pids["Tmdb"] = str(tmdb)
    if imdb is not None:
        pids["Imdb"] = imdb
    if tvdb is not None:
        pids["Tvdb"] = str(tvdb)
    return JellyfinItem(id=jid, name=jid, provider_ids=pids)


def test_index_matches_by_tmdb_then_imdb():
    index = LibraryIndex([_item("a", tmdb=9012), _item("b", imdb="tt9000006")])
    # tmdb hit
    assert index.find(MediaItem(title="Film One", tmdb_id=9012)).id == "a"
    # imdb fallback (no tmdb on the wanted movie's match)
    assert (
        index.find(MediaItem(title="Film Two", tmdb_id=999, imdb_id="tt9000006")).id
        == "b"
    )
    # no match
    assert index.find(MediaItem(title="No Match", tmdb_id=555)) is None


def test_index_prefers_tvdb_then_falls_back_for_shows():
    index = LibraryIndex([_item("s", tvdb=121361, tmdb=1396)])
    # a show with a tvdb id matches by Tvdb...
    assert index.find(MediaItem(title="BB", tvdb_id=121361, media_type="tv")).id == "s"
    # ...and tmdb still resolves it when only tmdb is known
    assert index.find(MediaItem(title="BB", tmdb_id=1396, media_type="tv")).id == "s"
    # a different tvdb id misses (no spurious cross-match)
    assert index.find(MediaItem(title="Other", tvdb_id=999, media_type="tv")) is None


def test_match_splits_present_and_missing():
    index = LibraryIndex([_item("have", tmdb=11)])
    result = index.match(
        [MediaItem(title="Have", tmdb_id=11), MediaItem(title="Missing", tmdb_id=22)]
    )
    assert [m.title for m, _ in result.matched] == ["Have"]
    assert [m.title for m in result.missing] == ["Missing"]


def test_media_routed_index_dispatches_by_media_type():
    # Movie 550 and show 550 are different items; the router must send each query to its
    # own sub-index so their overlapping tmdb ids never cross-match.
    routed = MediaRoutedIndex(
        {
            "movie": LibraryIndex([_item("the-movie", tmdb=550)]),
            "tv": LibraryIndex([_item("the-show", tmdb=550)]),
        }
    )
    assert (
        routed.find(MediaItem(title="Fight Club", tmdb_id=550, media_type="movie")).id
        == "the-movie"
    )
    assert (
        routed.find(MediaItem(title="A Show", tmdb_id=550, media_type="tv")).id
        == "the-show"
    )
    assert routed.size == 2


def test_media_routed_index_missing_media_returns_none():
    routed = MediaRoutedIndex({"movie": LibraryIndex([_item("m", tmdb=1)])})
    # A tv query with no tv sub-index resolves to nothing rather than erroring.
    assert routed.find(MediaItem(title="Show", tmdb_id=1, media_type="tv")) is None


def test_get_series_requests_series_item_type():
    client = JellyfinClient("http://host:8096", "k")
    calls: list = []

    def fake_get(path, params=None, **kw):
        calls.append((path, params))
        return {"Items": [], "TotalRecordCount": 0}

    client.get = fake_get
    client.get_series(["libA"])
    assert calls and calls[0][0] == "Items"
    assert calls[0][1]["includeItemTypes"] == "Series"
    assert calls[0][1]["parentId"] == "libA"


def test_add_to_collection_batches_member_ids():
    # All ids in one query string overflows the URL limit (HTTP 414) for large
    # collections.
    client = JellyfinClient("http://host:8096", "k")
    calls: list = []

    def fake_post(path, *, params=None, json=None):
        calls.append((path, params))

    client.post = fake_post
    ids = [f"id{i:03d}" for i in range(120)]
    client.add_to_collection("coll1", ids)
    # 120 ids, batch 50 -> 3 calls sized 50, 50, 20; every id sent once, in order.
    assert [len(p["ids"].split(",")) for _, p in calls] == [50, 50, 20]
    assert [i for _, p in calls for i in p["ids"].split(",")] == ids
    assert all(path == "Collections/coll1/Items" for path, _ in calls)


def test_remove_from_collection_batches_member_ids():
    client = JellyfinClient("http://host:8096", "k")
    calls: list = []

    def fake_delete(path, *, params=None):
        calls.append((path, params))

    client.delete = fake_delete
    client.remove_from_collection("coll2", [f"id{i}" for i in range(75)])
    assert [len(p["ids"].split(",")) for _, p in calls] == [50, 25]


def test_add_to_collection_empty_is_noop():
    client = JellyfinClient("http://host:8096", "k")
    calls: list = []
    client.post = lambda *a, **k: calls.append(a)
    client.add_to_collection("c", [])
    assert calls == []
