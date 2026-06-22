"""Unit tests for the builders layer (no network; clients are stubbed)."""

from __future__ import annotations

from nalanda.builders import _as_list, dedupe_movies, run_builders
from nalanda.config import BuilderBlock, CollectionDef
from nalanda.models import MediaItem, MovieCollection


def test_as_list():
    assert _as_list(None) == []
    assert _as_list(119) == [119]
    assert _as_list([1, 2]) == [1, 2]


def test_identity_keys_are_media_scoped():
    m = MediaItem(title="x", tmdb_id=5, tvdb_id=7, imdb_id="tt1", media_type="tv")
    assert set(m.identity_keys()) == {
        ("tv", "tmdb", 5),
        ("tv", "tvdb", 7),
        ("tv", "imdb", "tt1"),
    }
    # a movie and a show sharing a numeric id don't collide
    assert MediaItem(title="movie", tmdb_id=5).identity_keys() == (
        ("movie", "tmdb", 5),
    )
    # an id-less item has no identity keys
    assert MediaItem(title="none").identity_keys() == ()


def test_intersect_movies_matches_on_any_shared_id():
    from nalanda.builders import _intersect_movies

    a = [
        MediaItem(title="x", tvdb_id=100, media_type="tv"),
        MediaItem(title="y", tvdb_id=101, media_type="tv"),
    ]
    b = [MediaItem(title="x2", tvdb_id=100, media_type="tv")]
    # the show shared by tvdb id survives even with no tmdb id on either side
    assert [m.tvdb_id for m in _intersect_movies([a, b])] == [100]


def _tvonly(title, tvdb, date="2010-01-01"):
    return MediaItem(title=title, tvdb_id=tvdb, media_type="tv", release_date=date)


class _FakeTVOnly:
    """TMDB tv stub returning items with a tvdb id but NO tmdb id
    (the TVDB-only case)."""

    def resolve_genre(self, name, *, media="movie"):
        return {"drama": 18}.get(name.casefold())

    def get_genre_shows(self, expr, *, limit=None):
        return [
            _tvonly("shared", 100, "2001-01-01"),
            _tvonly("genre-only", 101, "2002"),
        ]

    def search_keyword(self, name):
        return 55

    def get_keyword_shows(self, expr, *, without_keywords=None, limit=None):
        return [_tvonly("shared", 100, "2001-01-01"), _tvonly("kw-only", 102, "2003")]


def test_match_all_keeps_tvdb_only_items():
    coll = CollectionDef(
        media="tv", tmdb_genre={"any": ["Drama"]}, tmdb_keyword=55, match="all"
    )
    r = run_builders("X", coll, tmdb=_FakeTVOnly(), mdblist=None)
    assert [m.tvdb_id for m in r.movies] == [100]  # only the show present in BOTH keys


def test_except_subtracts_tvdb_only_items():
    coll = CollectionDef(
        media="tv", tmdb_genre={"any": ["Drama"]}, tmdb_keyword={"except": [55]}
    )
    r = run_builders("X", coll, tmdb=_FakeTVOnly(), mdblist=None)
    tvdb_ids = [m.tvdb_id for m in r.movies]
    assert 100 not in tvdb_ids  # the excepted tvdb-only show is removed
    assert 101 in tvdb_ids  # the genre-only show stays


def test_dedupe_by_tmdb_preserves_order():
    movies = [
        MediaItem(title="A", tmdb_id=1),
        MediaItem(title="A-dup", tmdb_id=1),
        MediaItem(title="B", tmdb_id=2),
    ]
    assert [m.tmdb_id for m in dedupe_movies(movies)] == [1, 2]


def test_dedupe_imdb_fallback_and_idless_kept():
    movies = [
        MediaItem(title="A", imdb_id="tt1"),
        MediaItem(title="A-dup", imdb_id="tt1"),
        MediaItem(title="No ids"),  # kept (can't dedupe without an id)
    ]
    out = dedupe_movies(movies)
    assert len(out) == 2


def test_dedupe_scoped_by_media_type():
    # A movie and a show sharing a numeric tmdb id are distinct titles (separate id
    # spaces) and must both survive; a same-media duplicate is still collapsed.
    items = [
        MediaItem(title="Fight Club", tmdb_id=550, media_type="movie"),
        MediaItem(title="Some Show", tmdb_id=550, media_type="tv"),
        MediaItem(title="Fight Club dup", tmdb_id=550, media_type="movie"),
    ]
    out = dedupe_movies(items)
    assert [(m.title, m.media_type) for m in out] == [
        ("Fight Club", "movie"),
        ("Some Show", "tv"),
    ]


class _FakeTMDB:
    def get_collection(self, cid):
        return MovieCollection(
            tmdb_id=cid,
            name=f"C{cid}",
            overview=f"overview-{cid}",
            poster_path=f"/p{cid}.jpg",
            backdrop_path=f"/b{cid}.jpg",
            thumb_path=f"/t{cid}.jpg",
            movies=[MediaItem(title=f"m{cid}", tmdb_id=cid * 10)],
        )


def test_run_builders_merge_has_no_source_metadata():
    # a MERGE has no single source -> blank overview/images/id
    # (config must supply metadata)
    coll = CollectionDef(media="movie", tmdb_collection=[1, 2])
    result = run_builders("X", coll, tmdb=_FakeTMDB(), mdblist=None)
    assert [m.tmdb_id for m in result.movies] == [10, 20]
    assert result.overview is None
    assert result.primary_url is None
    assert result.thumb_url is None
    assert result.backdrop_url is None
    assert result.tmdb_collection_id is None


def test_run_builders_single_collection_has_metadata_and_id():
    coll = CollectionDef(media="movie", tmdb_collection=1)
    result = run_builders("X", coll, tmdb=_FakeTMDB(), mdblist=None)
    assert result.overview == "overview-1"
    assert result.primary_url.endswith("/p1.jpg")
    assert result.thumb_url.endswith("/t1.jpg")
    assert result.backdrop_url.endswith("/b1.jpg")
    assert result.tmdb_collection_id == 1


def test_tmdb_overview_fills_merge_overview():
    # a MERGE has no sole source; tmdb_overview fetches a named collection's overview
    coll = CollectionDef(media="movie", tmdb_collection=[1, 2], tmdb_overview=2)
    result = run_builders("X", coll, tmdb=_FakeTMDB(), mdblist=None)
    assert [m.tmdb_id for m in result.movies] == [10, 20]
    assert result.overview == "overview-2"  # fetched at runtime
    assert result.tmdb_collection_id is None  # still a merge (Nalanda-owned)


def test_tmdb_overview_overrides_sole_collection_overview():
    coll = CollectionDef(media="movie", tmdb_collection=1, tmdb_overview=99)
    result = run_builders("X", coll, tmdb=_FakeTMDB(), mdblist=None)
    assert result.overview == "overview-99"  # tmdb_overview wins over the sole source
    assert result.tmdb_collection_id == 1  # still Jellyfin-managed (one collection)


class _FakeTMDBDated:
    # collection 1 returns movies OUT of order; collection 2 interleaves date-wise
    DATA = {
        1: [(338953, "2022-04-06"), (259316, "2016-11-16")],
        2: [(338952, "2018-11-14")],
    }

    def get_collection(self, cid):
        movies = [
            MediaItem(title=str(t), tmdb_id=t, release_date=d)
            for t, d in self.DATA[cid]
        ]
        return MovieCollection(tmdb_id=cid, name=f"C{cid}", movies=movies)


def test_merge_sorts_combined_by_release_date():
    # combine BOTH ids first, then sort -> strict chronology across the merge
    coll = CollectionDef(media="movie", tmdb_collection=[1, 2])
    result = run_builders("X", coll, tmdb=_FakeTMDBDated(), mdblist=None)
    assert [m.release_date for m in result.movies] == [
        "2016-11-16",
        "2018-11-14",
        "2022-04-06",
    ]


class _FakeTMDBMixed:
    def get_collection(self, cid):
        return MovieCollection(
            tmdb_id=cid,
            name=f"C{cid}",
            movies=[
                MediaItem(title="early", tmdb_id=cid * 10, release_date="2016-01-01"),
                MediaItem(
                    title="late", tmdb_id=cid * 10 + 1, release_date="2022-01-01"
                ),
            ],
        )

    def get_movie(self, mid):
        return MediaItem(title=f"mv{mid}", tmdb_id=mid, release_date="2019-01-01")


def test_tmdb_movie_combines_and_sorts_chronologically():
    # collection (2016, 2022) + individual movie (2019) -> sorted: 2016, 2019, 2022
    coll = CollectionDef(media="movie", tmdb_collection=1, tmdb_movie=999)
    result = run_builders("X", coll, tmdb=_FakeTMDBMixed(), mdblist=None)
    assert [m.release_date for m in result.movies] == [
        "2016-01-01",
        "2019-01-01",
        "2022-01-01",
    ]
    # individual movies make it a custom collection -> no single TMDB id
    assert result.tmdb_collection_id is None


class _FakeTMDBKeyword:
    def get_keyword_movies(self, kid, *, without_keywords=None, limit=None):
        return [
            MediaItem(title="x", tmdb_id=5, release_date="2008-01-01"),
            MediaItem(title="y", tmdb_id=6, release_date="2012-01-01"),
        ]

    def get_movie(self, mid):
        return MediaItem(title="m", tmdb_id=mid, release_date="2010-01-01")


class _FakeKWSearch:
    def search_keyword(self, q):
        return 1234 if "keyword" in q.lower() else None


def test_keyword_id_accepts_id_numeric_string_and_name():
    from nalanda.builders import _keyword_id

    fake = _FakeKWSearch()
    assert _keyword_id("X", 1234, fake) == 1234  # int id
    assert _keyword_id("X", "1234", fake) == 1234  # numeric string -> id
    assert _keyword_id("X", "Some Keyword", fake) == 1234  # name -> search


def test_keyword_name_not_found_raises():
    import pytest

    from nalanda.builders import _keyword_id

    class _F:
        def search_keyword(self, q):
            return None

    with pytest.raises(ValueError):
        _keyword_id("X", "no such keyword", _F())


def test_tmdb_keyword_joins_chronological_pool():
    # keyword movies (2008, 2012) + individual movie (2010) -> chronological
    coll = CollectionDef(media="movie", tmdb_keyword=1234, tmdb_movie=99)
    result = run_builders("X", coll, tmdb=_FakeTMDBKeyword(), mdblist=None)
    assert [m.release_date for m in result.movies] == [
        "2008-01-01",
        "2010-01-01",
        "2012-01-01",
    ]
    assert result.tmdb_collection_id is None


class _FakeTMDBGenre:
    GENRES = {"western": 37, "action": 28, "adventure": 12, "horror": 27, "comedy": 35}

    def __init__(self):
        self.last_with_genres = None

    def resolve_genre(self, name, *, media="movie"):
        return self.GENRES.get(name.casefold())

    def get_genre_movies(self, with_genres, *, limit=None):
        self.last_with_genres = with_genres
        return [
            MediaItem(title="g1", tmdb_id=101, release_date="2001-01-01"),
            MediaItem(title="g2", tmdb_id=102, release_date="2003-01-01"),
        ]

    def get_movie(self, mid):
        return MediaItem(title="mv", tmdb_id=mid, release_date="2002-01-01")


def test_genre_id_accepts_id_numeric_string_and_name():
    from nalanda.builders import _genre_id

    fake = _FakeTMDBGenre()
    assert _genre_id("X", 37, fake.resolve_genre) == 37  # int id
    assert _genre_id("X", "37", fake.resolve_genre) == 37  # numeric string -> id
    assert _genre_id("X", "Western", fake.resolve_genre) == 37  # name -> resolved


def test_genre_name_not_found_raises():
    import pytest

    from nalanda.builders import _genre_id

    with pytest.raises(ValueError):
        _genre_id("X", "no such genre", _FakeTMDBGenre().resolve_genre)


def test_genre_ids_operator_defaults_and_explicit():
    from nalanda.builders import _genre_ids
    from nalanda.config import SelectFilter

    resolve = _FakeTMDBGenre().resolve_genre
    assert _genre_ids("X", "Western", resolve) == (",", [37])  # scalar -> AND (one id)
    assert _genre_ids("X", ["Action", "Adventure"], resolve) == (
        ",",
        [28, 12],
    )  # bare list -> AND
    assert _genre_ids("X", SelectFilter(all=["Action", 12]), resolve) == (
        ",",
        [28, 12],
    )  # mixed ok
    assert _genre_ids("X", SelectFilter(any=["Horror", "Comedy"]), resolve) == (
        "|",
        [27, 35],
    )  # OR


def test_genre_joins_chronological_pool_with_and_join():
    fake = _FakeTMDBGenre()
    # genre movies (2001, 2003) + individual movie (2002) -> chronological
    coll = CollectionDef(
        media="movie", tmdb_genre=["Action", "Adventure"], tmdb_movie=99
    )
    result = run_builders("X", coll, tmdb=fake, mdblist=None)
    assert [m.release_date for m in result.movies] == [
        "2001-01-01",
        "2002-01-01",
        "2003-01-01",
    ]
    assert fake.last_with_genres == "28,12"  # bare list -> AND join
    assert result.tmdb_collection_id is None


def test_genre_any_uses_or_join():
    fake = _FakeTMDBGenre()
    coll = CollectionDef(
        media="movie", tmdb_genre={"any": ["Horror", "Comedy"]}
    )  # dict -> SelectFilter
    run_builders("X", coll, tmdb=fake, mdblist=None)
    assert fake.last_with_genres == "27|35"  # OR join


def test_genre_filter_requires_exactly_one_operator():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CollectionDef(
            media="movie", tmdb_genre={"all": [1], "any": [2]}
        )  # both include modes
    with pytest.raises(ValidationError):
        CollectionDef(media="movie", tmdb_genre={})  # nothing set


def test_genre_filter_allows_except():
    from nalanda.config import SelectFilter

    SelectFilter(**{"except": ["Documentary"]})  # except-only is valid
    SelectFilter(
        all=["Action"], **{"except": ["Animation"]}
    )  # include + except is valid


class _FakeTMDBExcept:
    def resolve_genre(self, name, *, media="movie"):
        return {"documentary": 99, "horror": 27}.get(name.casefold())

    def get_keyword_movies(self, kid, *, without_keywords=None, limit=None):
        return [
            MediaItem(
                title="film", tmdb_id=1, release_date="2010-01-01", genre_ids=[28, 12]
            ),
            MediaItem(
                title="doc", tmdb_id=2, release_date="2011-01-01", genre_ids=[99]
            ),
            MediaItem(
                title="docmix", tmdb_id=3, release_date="2012-01-01", genre_ids=[18, 99]
            ),
        ]

    def get_list(
        self, lid, *, media="movie"
    ):  # curated (non-chronological) order; tmdb_id 1 is horror
        return [
            MediaItem(title="c", tmdb_id=30, release_date="2020-01-01", genre_ids=[18]),
            MediaItem(title="a", tmdb_id=31, release_date="2000-01-01", genre_ids=[27]),
            MediaItem(title="b", tmdb_id=32, release_date="2010-01-01", genre_ids=[35]),
        ]


def test_genre_except_filters_out_excepted_genre():
    # keyword source + genre except Documentary -> drop genre 99
    coll = CollectionDef(
        media="movie", tmdb_keyword=1234, tmdb_genre={"except": ["Documentary"]}
    )
    result = run_builders("X", coll, tmdb=_FakeTMDBExcept(), mdblist=None)
    assert [m.tmdb_id for m in result.movies] == [1]  # the two documentaries dropped
    assert result.tmdb_collection_id is None


def test_except_only_keeps_curated_order_and_filters():
    # a sole list + an except-only genre filter keeps curated order, just drops matches
    coll = CollectionDef(
        media="movie", tmdb_list=1234, tmdb_genre={"except": ["Horror"]}
    )
    result = run_builders("X", coll, tmdb=_FakeTMDBExcept(), mdblist=None)
    assert [m.tmdb_id for m in result.movies] == [
        30,
        32,
    ]  # curated order; horror (31) dropped


class _FakeTMDBTitle:
    def __init__(self):
        self.calls = []

    def find_movie_by_title(self, title, *, year=None):
        self.calls.append((title, year))
        if (
            title == "Remade Film" and year is None
        ):  # bare title -> TMDB's first match (the newer one)
            return MediaItem(
                title="Remade Film", tmdb_id=9004, release_date="2017-06-06"
            )
        known = {
            ("Remade Film", 1999): MediaItem(
                title="Remade Film", tmdb_id=9005, release_date="1999-05-07"
            ),
            ("Example Film", None): MediaItem(
                title="Example Film", tmdb_id=9006, release_date="2010-07-16"
            ),
            ("9999", None): MediaItem(
                title="9999", tmdb_id=9007, release_date="2009-11-13"
            ),
        }
        return known.get((title, year))


def test_split_title_year():
    from nalanda.builders import _split_title_year

    assert _split_title_year("Example Film") == ("Example Film", None)
    assert _split_title_year("Remade Film (1999)") == ("Remade Film", 1999)
    assert _split_title_year("A Film 2049") == (
        "A Film 2049",
        None,
    )  # no parens


def test_title_resolves_year_disambiguates_and_sorts_chronologically():
    fake = _FakeTMDBTitle()
    # numeric title (int from YAML) + year-qualified + plain -> chronological
    coll = CollectionDef(
        media="movie", tmdb_title=[9999, "Remade Film (1999)", "Example Film"]
    )
    result = run_builders("X", coll, tmdb=fake, mdblist=None)
    assert [m.tmdb_id for m in result.movies] == [
        9005,
        9007,
        9006,
    ]  # 1999, 2009, 2010 sorted
    assert ("Remade Film", 1999) in fake.calls  # year was parsed and passed through
    assert ("9999", None) in fake.calls  # int stringified
    assert result.tmdb_collection_id is None  # titles -> custom collection


def test_title_not_found_raises():
    import pytest

    coll = CollectionDef(media="movie", tmdb_title="zzxq no such film")
    with pytest.raises(ValueError):
        run_builders("X", coll, tmdb=_FakeTMDBTitle(), mdblist=None)


class _FakeTMDBList:
    def get_list(self, lid, *, media="movie"):
        # returns movies in a NON-chronological (curated) order
        return [
            MediaItem(title="c", tmdb_id=3, release_date="2020-01-01"),
            MediaItem(title="a", tmdb_id=1, release_date="2000-01-01"),
            MediaItem(title="b", tmdb_id=2, release_date="2010-01-01"),
        ]


def test_tmdb_list_alone_preserves_curated_order():
    # a SOLE single list keeps its curated order
    coll = CollectionDef(media="movie", tmdb_list=1234)
    result = run_builders("X", coll, tmdb=_FakeTMDBList(), mdblist=None)
    assert [m.tmdb_id for m in result.movies] == [
        3,
        1,
        2,
    ]  # list order, NOT release-date sorted
    assert result.tmdb_collection_id is None


class _FakeMixed:
    def get_collection(self, cid):
        return MovieCollection(
            tmdb_id=cid,
            name="c",
            movies=[MediaItem(title="coll", tmdb_id=1, release_date="2005-01-01")],
        )

    def get_list(self, lid, *, media="movie"):
        return [
            MediaItem(title="L-late", tmdb_id=2, release_date="2010-01-01"),
            MediaItem(title="L-early", tmdb_id=3, release_date="2000-01-01"),
        ]


def test_list_combined_with_collection_is_chronological():
    # list + collection -> curated order dropped, everything interleaves by release date
    coll = CollectionDef(media="movie", tmdb_collection=1, tmdb_list=99)
    result = run_builders("X", coll, tmdb=_FakeMixed(), mdblist=None)
    assert [m.release_date for m in result.movies] == [
        "2000-01-01",
        "2005-01-01",
        "2010-01-01",
    ]


class _FakeAll:
    def get_list(self, lid, *, media="movie"):  # curated order
        return [
            MediaItem(title="L3", tmdb_id=30, release_date="2020-01-01"),
            MediaItem(title="L1", tmdb_id=31, release_date="2000-01-01"),
            MediaItem(title="L2", tmdb_id=32, release_date="2010-01-01"),
        ]

    def get_collection(self, cid):
        return MovieCollection(
            tmdb_id=cid,
            name="c",
            movies=[
                MediaItem(title="C2", tmdb_id=20, release_date="2002-01-01"),
                MediaItem(title="C1", tmdb_id=21, release_date="2001-01-01"),
            ],
        )

    def get_movie(self, mid):
        return MediaItem(title="M", tmdb_id=mid, release_date="1990-01-01")


def test_append_blocks_concatenate_in_order():
    # block 0: a sole list (curated); then a collection block (chronological);
    # then a movie
    coll = CollectionDef(
        media="movie",
        tmdb_list=1234,
        append=[BuilderBlock(tmdb_collection=1), BuilderBlock(tmdb_movie=99)],
    )
    result = run_builders("X", coll, tmdb=_FakeAll(), mdblist=None)
    # [list curated 30,31,32] + [collection chronological 21@2001, 20@2002] + [movie 99]
    assert [m.tmdb_id for m in result.movies] == [30, 31, 32, 21, 20, 99]
    assert result.tmdb_collection_id is None  # append present -> custom collection


def test_run_builders_errors_without_required_client():
    import pytest

    coll = CollectionDef(media="movie", mdblist_list="user/list")
    with pytest.raises(ValueError):
        run_builders("X", coll, tmdb=None, mdblist=None)


# --- people builders --------------------------------------------------------
class _FakeTMDBPerson:
    def __init__(self):
        self.last = None

    def search_person(self, name):
        return {"a director": 525, "an actor": 500}.get(name.casefold())

    def resolve_genre(self, name, *, media="movie"):
        return 99 if name.casefold() == "documentary" else None

    def get_person_movies(self, person_id, *, cast=False, department=None):
        self.last = (person_id, cast, department)
        if cast:
            return [MediaItem(title="act", tmdb_id=1, release_date="2001-01-01")]
        # crew: a writer+director double-credit on one film (dup) + a documentary
        return [
            MediaItem(
                title="dir1", tmdb_id=10, release_date="2010-01-01", genre_ids=[18]
            ),
            MediaItem(
                title="dir1-dup", tmdb_id=10, release_date="2010-01-01", genre_ids=[18]
            ),
            MediaItem(
                title="doc", tmdb_id=12, release_date="2012-01-01", genre_ids=[99]
            ),
        ]


def test_actor_resolves_name_and_joins_chronological():
    fake = _FakeTMDBPerson()
    result = run_builders(
        "X",
        CollectionDef(media="movie", tmdb_actor="An Actor"),
        tmdb=fake,
        mdblist=None,
    )
    assert fake.last == (500, True, None)  # resolved id, acting credits
    assert [m.tmdb_id for m in result.movies] == [1]
    assert result.tmdb_collection_id is None


def test_director_filters_by_department_and_dedupes():
    fake = _FakeTMDBPerson()
    result = run_builders(
        "X",
        CollectionDef(media="movie", tmdb_director="A Director"),
        tmdb=fake,
        mdblist=None,
    )
    assert fake.last == (525, False, "Directing")  # crew, Directing department
    assert [m.tmdb_id for m in result.movies] == [
        10,
        12,
    ]  # deduped (10 once), chronological


def test_director_with_genre_except_drops_documentary():
    coll = CollectionDef(
        media="movie",
        tmdb_director="A Director",
        tmdb_genre={"except": ["Documentary"]},
    )
    result = run_builders("X", coll, tmdb=_FakeTMDBPerson(), mdblist=None)
    assert [m.tmdb_id for m in result.movies] == [
        10
    ]  # the doc (genre 99) is filtered out


def test_person_id_numeric_skips_search():
    fake = (
        _FakeTMDBPerson()
    )  # search_person would KeyError-return None for unknown; not called
    run_builders(
        "X", CollectionDef(media="movie", tmdb_actor=500), tmdb=fake, mdblist=None
    )
    assert fake.last == (500, True, None)


# --- company builder --------------------------------------------------------
class _FakeTMDBCompany:
    def search_company(self, name):
        return 41077 if name.casefold() == "a studio" else None

    def get_company_movies(self, company_id, *, limit=None):
        return [
            MediaItem(title="x", tmdb_id=1, release_date="2017-01-01"),
            MediaItem(title="y", tmdb_id=2, release_date="2015-01-01"),
        ]


def test_company_resolves_name_and_is_chronological():
    result = run_builders(
        "X",
        CollectionDef(media="movie", tmdb_company="A Studio"),
        tmdb=_FakeTMDBCompany(),
        mdblist=None,
    )
    assert [m.tmdb_id for m in result.movies] == [2, 1]  # release-sorted (2015, 2017)
    assert result.tmdb_collection_id is None


# --- chart builders ---------------------------------------------------------
class _FakeTMDBCharts:
    def __init__(self):
        self.last = None

    def get_chart(self, chart, limit):
        self.last = (chart, limit)
        movies = [  # deliberately NON-chronological "chart order"
            MediaItem(title="c", tmdb_id=3, release_date="2020-01-01"),
            MediaItem(title="a", tmdb_id=1, release_date="2000-01-01"),
            MediaItem(title="b", tmdb_id=2, release_date="2010-01-01"),
        ]
        return movies[:limit]

    def get_collection(self, cid):
        return MovieCollection(
            tmdb_id=cid,
            name="c",
            movies=[MediaItem(title="m", tmdb_id=99, release_date="2005-01-01")],
        )


def test_sole_chart_keeps_server_order():
    fake = _FakeTMDBCharts()
    result = run_builders(
        "X", CollectionDef(media="movie", tmdb_popular=3), tmdb=fake, mdblist=None
    )
    assert fake.last == ("popular", 3)
    assert [m.tmdb_id for m in result.movies] == [
        3,
        1,
        2,
    ]  # chart order, NOT release-sorted


def test_chart_plus_collection_is_chronological():
    coll = CollectionDef(media="movie", tmdb_top_rated=3, tmdb_collection=1)
    result = run_builders("X", coll, tmdb=_FakeTMDBCharts(), mdblist=None)
    assert [m.release_date for m in result.movies] == [
        "2000-01-01",
        "2005-01-01",
        "2010-01-01",
        "2020-01-01",
    ]


# --- discover escape hatch --------------------------------------------------
class _FakeTMDBDiscover:
    def __init__(self):
        self.last = None

    def discover_movies(self, filters, *, limit=None):
        self.last = (dict(filters), limit)
        movies = [
            MediaItem(title="d1", tmdb_id=1, release_date="2020-01-01"),
            MediaItem(title="d2", tmdb_id=2, release_date="2000-01-01"),
        ]
        return movies if limit is None else movies[:limit]


def test_discover_extracts_limit_and_drops_page():
    fake = _FakeTMDBDiscover()
    coll = CollectionDef(
        media="movie",
        tmdb_discover={"with_genres": 878, "sort_by": "x", "limit": 5, "page": 9},
    )
    run_builders("X", coll, tmdb=fake, mdblist=None)
    assert fake.last == (
        {"with_genres": 878, "sort_by": "x"},
        5,
    )  # limit + page intercepted


def test_sole_discover_preserves_sort_order():
    # a sole discover keeps server order (NOT release-sorted: d1@2020 before d2@2000)
    result = run_builders(
        "X",
        CollectionDef(media="movie", tmdb_discover={"sort_by": "vote_average.desc"}),
        tmdb=_FakeTMDBDiscover(),
        mdblist=None,
    )
    assert [m.tmdb_id for m in result.movies] == [1, 2]
    assert result.tmdb_collection_id is None


# --- all/any/except operators for people & company (client-side set algebra) --
class _FakeTMDBOps:
    PEOPLE = {500: [(1, 10.0), (2, 5.0), (3, 1.0)], 600: [(2, 5.0), (3, 1.0), (4, 8.0)]}
    COMPANIES = {41077: [(1, 9.0), (2, 3.0)], 222: [(2, 3.0), (5, 7.0)]}

    def search_person(self, name):
        return {"a": 500, "b": 600}.get(name.casefold())

    def search_company(self, name):
        return {"x": 41077, "y": 222}.get(name.casefold())

    @staticmethod
    def _films(data, key):
        return [
            MediaItem(
                title=f"f{t}", tmdb_id=t, release_date=f"20{t:02d}-06-01", popularity=p
            )
            for t, p in data[key]
        ]

    def get_person_movies(self, pid, *, cast=False, department=None):
        return self._films(self.PEOPLE, pid)

    def get_company_movies(self, cid, *, limit=None):
        return self._films(self.COMPANIES, cid)


def test_people_any_unions():
    r = run_builders(
        "X",
        CollectionDef(media="movie", tmdb_actor={"any": [500, 600]}),
        tmdb=_FakeTMDBOps(),
        mdblist=None,
    )
    assert [m.tmdb_id for m in r.movies] == [
        1,
        2,
        3,
        4,
    ]  # union, deduped, chronological


def test_people_all_intersects():
    r = run_builders(
        "X",
        CollectionDef(media="movie", tmdb_actor={"all": [500, 600]}),
        tmdb=_FakeTMDBOps(),
        mdblist=None,
    )
    assert [m.tmdb_id for m in r.movies] == [2, 3]  # films with BOTH


def test_people_except_subtracts():
    # include 500's films {1,2,3}, exclude 600's films {2,3,4} -> {1}
    coll = CollectionDef(media="movie", tmdb_actor={"any": [500], "except": [600]})
    r = run_builders("X", coll, tmdb=_FakeTMDBOps(), mdblist=None)
    assert [m.tmdb_id for m in r.movies] == [1]


def test_people_bare_list_is_union():
    r = run_builders(
        "X",
        CollectionDef(media="movie", tmdb_director=[500, 600]),
        tmdb=_FakeTMDBOps(),
        mdblist=None,
    )
    assert [m.tmdb_id for m in r.movies] == [1, 2, 3, 4]


def test_company_all_intersects():
    r = run_builders(
        "X",
        CollectionDef(media="movie", tmdb_company={"all": ["X", "Y"]}),
        tmdb=_FakeTMDBOps(),
        mdblist=None,
    )
    assert [m.tmdb_id for m in r.movies] == [2]


# --- collection-level limit -----------------------------------------------------------
def test_limit_caps_people_by_popularity():
    # person 500: f1(pop10), f2(pop5), f3(pop1) -> top 2 popular = f1,f2
    # -> chronological [1,2]
    r = run_builders(
        "X",
        CollectionDef(media="movie", tmdb_actor=500, limit=2),
        tmdb=_FakeTMDBOps(),
        mdblist=None,
    )
    assert [m.tmdb_id for m in r.movies] == [1, 2]


class _FakeKwLimit:
    def __init__(self):
        self.limit = "unset"

    def get_keyword_movies(self, kid, *, without_keywords=None, limit=None):
        self.limit = limit
        return [MediaItem(title="k", tmdb_id=1, release_date="2000-01-01")]


def test_limit_passed_to_keyword_fetch():
    fake = _FakeKwLimit()
    run_builders(
        "X",
        CollectionDef(media="movie", tmdb_keyword=5, limit=25),
        tmdb=fake,
        mdblist=None,
    )
    assert fake.limit == 25  # collection limit caps the broad query fetch


def test_default_limit_applied_when_unset():
    fake = _FakeKwLimit()
    run_builders(
        "X", CollectionDef(media="movie", tmdb_keyword=5), tmdb=fake, mdblist=None
    )  # no limit:
    assert fake.limit == 100  # the default cap


def test_limit_zero_means_unlimited():
    fake = _FakeKwLimit()
    run_builders(
        "X",
        CollectionDef(media="movie", tmdb_keyword=5, limit=0),
        tmdb=fake,
        mdblist=None,
    )
    assert fake.limit is None  # 0 -> unlimited (no cap)


# --- MDBList builders (list operators, catalog, official) -----------------------------
class _FakeMDBList:
    LISTS = {
        "a": [(3, "2003"), (1, "2001"), (2, "2002")],
        "b": [(2, "2002"), (3, "2003"), (4, "2004")],
    }

    def __init__(self):
        self.last_catalog = None
        self.last_official = None

    def get_list(
        self, ref, *, sort_by=None, limit=None, genre_resolver=None, media="movie"
    ):
        items = self.LISTS[ref]
        return [
            MediaItem(title=f"l{t}", tmdb_id=t, release_date=f"{d}-01-01")
            for t, d in items
        ][:limit]

    def get_catalog(self, filters, *, limit=None, genre_resolver=None, media="movie"):
        self.last_catalog = (dict(filters), limit)
        movies = [
            MediaItem(title=f"c{i}", tmdb_id=10 + i, release_date=f"20{i:02d}-01-01")
            for i in range(5)
        ]
        return movies[:limit] if limit else movies

    def get_official_movies(
        self, slug, *, sort_by=None, limit=None, genre_resolver=None, media="movie"
    ):
        self.last_official = (slug, sort_by, limit)
        return [MediaItem(title="o", tmdb_id=77, release_date="2020-01-01")]


class _FakeTMDBColl:
    def get_collection(self, cid):
        return MovieCollection(
            tmdb_id=cid,
            name="c",
            movies=[MediaItem(title="m", tmdb_id=99, release_date="2005-01-01")],
        )

    def get_movie(self, mid):
        return MediaItem(title="mv", tmdb_id=mid, release_date="2002-01-01")


def test_mdblist_sole_list_keeps_curated_order():
    r = run_builders(
        "X",
        CollectionDef(media="movie", mdblist_list="a"),
        tmdb=None,
        mdblist=_FakeMDBList(),
    )
    assert [m.tmdb_id for m in r.movies] == [
        3,
        1,
        2,
    ]  # list-a server order, NOT release-sorted


def test_release_sorted_flag_drives_default_order():
    # single collection + merge are release-sorted -> default order would be
    # release_date
    assert run_builders(
        "X",
        CollectionDef(media="movie", tmdb_collection=1),
        tmdb=_FakeTMDB(),
        mdblist=None,
    ).release_sorted
    assert run_builders(
        "X",
        CollectionDef(media="movie", tmdb_collection=[1, 2]),
        tmdb=_FakeTMDB(),
        mdblist=None,
    ).release_sorted
    # a sole curated list keeps server order -> NOT release-sorted
    # -> default order source
    assert not run_builders(
        "X",
        CollectionDef(media="movie", mdblist_list="a"),
        tmdb=None,
        mdblist=_FakeMDBList(),
    ).release_sorted
    # a list mixed with a collection is release-sorted again
    assert run_builders(
        "X",
        CollectionDef(media="movie", mdblist_list="a", tmdb_collection=1),
        tmdb=_FakeTMDBColl(),
        mdblist=_FakeMDBList(),
    ).release_sorted


def test_mdblist_url_union():
    r = run_builders(
        "X",
        CollectionDef(media="movie", mdblist_list={"url": ["a", "b"]}),
        tmdb=None,
        mdblist=_FakeMDBList(),
    )
    assert [m.tmdb_id for m in r.movies] == [3, 1, 2, 4]  # union, deduped, in order


def test_mdblist_all_intersects():
    r = run_builders(
        "X",
        CollectionDef(media="movie", mdblist_list={"all": ["a", "b"]}),
        tmdb=None,
        mdblist=_FakeMDBList(),
    )
    assert [m.tmdb_id for m in r.movies] == [3, 2]  # a ∩ b = {2,3} in a's order


def test_mdblist_except_subtracts():
    coll = CollectionDef(media="movie", mdblist_list={"url": "a", "except": ["b"]})
    r = run_builders("X", coll, tmdb=None, mdblist=_FakeMDBList())
    assert [m.tmdb_id for m in r.movies] == [1]  # a[3,1,2] minus b{2,3,4}


def test_mdblist_filter_allows_except_only():
    import pytest
    from pydantic import ValidationError

    from nalanda.config import MdblistFilter

    MdblistFilter(**{"except": ["b"]})  # except-only is valid (subtracts from the pool)
    MdblistFilter(url="a", **{"except": ["b"]})  # url + except is valid
    MdblistFilter(all=["a"], **{"except": ["b"]})  # all + except is valid
    with pytest.raises(ValidationError):
        MdblistFilter(url="a", all=["b"])  # can't union and intersect at once
    with pytest.raises(ValidationError):
        MdblistFilter()  # nothing set


def test_mdblist_plus_collection_chronological():
    coll = CollectionDef(media="movie", mdblist_list="a", tmdb_collection=1)
    r = run_builders("X", coll, tmdb=_FakeTMDBColl(), mdblist=_FakeMDBList())
    assert [m.release_date for m in r.movies] == [
        "2001-01-01",
        "2002-01-01",
        "2003-01-01",
        "2005-01-01",
    ]


def test_mdblist_catalog_honors_limit_and_filters():
    fake = _FakeMDBList()
    coll = CollectionDef(
        media="movie",
        mdblist_catalog={"sort": "rtomatoes", "score_min": 80, "limit": 3},
    )
    r = run_builders("X", coll, tmdb=None, mdblist=fake)
    assert len(r.movies) == 3
    assert fake.last_catalog[1] == 3
    assert (
        fake.last_catalog[0]["sort"] == "rtomatoes"
        and fake.last_catalog[0]["score_min"] == 80
    )


def test_catalog_inherits_collection_limit():
    fake = _FakeMDBList()
    run_builders(
        "X",
        CollectionDef(media="movie", mdblist_catalog={"sort": "score"}, limit=2),
        tmdb=None,
        mdblist=fake,
    )
    assert fake.last_catalog[1] == 2  # collection limit when catalog has none


def test_collection_plus_catalog_is_custom():
    coll = CollectionDef(
        media="movie", tmdb_collection=1, mdblist_catalog={"sort": "score"}
    )
    r = run_builders("X", coll, tmdb=_FakeTMDBColl(), mdblist=_FakeMDBList())
    assert r.tmdb_collection_id is None


def test_mdblist_official_string_and_model():
    fake = _FakeMDBList()
    r = run_builders(
        "X",
        CollectionDef(media="movie", mdblist_official="popular"),
        tmdb=None,
        mdblist=fake,
    )
    assert fake.last_official == ("popular", None, None)
    assert [m.tmdb_id for m in r.movies] == [77]
    fake2 = _FakeMDBList()
    run_builders(
        "X",
        CollectionDef(
            media="movie",
            mdblist_official={"slug": "popular", "sort_by": "score.desc", "limit": 5},
        ),
        tmdb=None,
        mdblist=fake2,
    )
    assert fake2.last_official == ("popular", "score.desc", 5)


def test_match_all_across_mdblist_and_movie():
    # list a {1,2,3} AND movie 2 -> intersection {2}
    coll = CollectionDef(media="movie", mdblist_list="a", tmdb_movie=2, match="all")
    r = run_builders("X", coll, tmdb=_FakeTMDBColl(), mdblist=_FakeMDBList())
    assert [m.tmdb_id for m in r.movies] == [2]


# --- keyword operators (server-side with_keywords / without_keywords) ---------
class _FakeKwOps:
    def __init__(self):
        self.calls = []

    def get_keyword_movies(self, with_keywords, *, without_keywords=None, limit=None):
        self.calls.append((with_keywords, without_keywords))
        return [MediaItem(title="k", tmdb_id=1, release_date="2000-01-01")]


def test_keyword_operators_build_discover_params():
    fake = _FakeKwOps()
    run_builders(
        "X",
        CollectionDef(media="movie", tmdb_keyword={"all": [1, 2]}),
        tmdb=fake,
        mdblist=None,
    )
    run_builders(
        "X",
        CollectionDef(media="movie", tmdb_keyword={"any": [1, 2]}),
        tmdb=fake,
        mdblist=None,
    )
    run_builders(
        "X", CollectionDef(media="movie", tmdb_keyword=[1, 2]), tmdb=fake, mdblist=None
    )  # bare list = OR
    run_builders(
        "X",
        CollectionDef(media="movie", tmdb_keyword={"any": [1], "except": [9]}),
        tmdb=fake,
        mdblist=None,
    )
    assert fake.calls == [("1,2", None), ("1|2", None), ("1|2", None), ("1", "9")]


class _FakeKwExceptOnly:
    def get_keyword_movies(self, with_keywords, *, without_keywords=None, limit=None):
        return [
            MediaItem(title="a", tmdb_id=2),
            MediaItem(title="b", tmdb_id=3),
        ]  # excepted keyword's films

    def get_collection(self, cid):
        return MovieCollection(
            tmdb_id=cid,
            name="c",
            movies=[
                MediaItem(title="x", tmdb_id=1, release_date="2001-01-01"),
                MediaItem(title="y", tmdb_id=2, release_date="2002-01-01"),
            ],
        )


def test_keyword_except_only_subtracts():
    # collection {1,2} + keyword except (films {2,3}) -> drop 2 -> {1}
    coll = CollectionDef(media="movie", tmdb_collection=1, tmdb_keyword={"except": [9]})
    r = run_builders("X", coll, tmdb=_FakeKwExceptOnly(), mdblist=None)
    assert [m.tmdb_id for m in r.movies] == [1]


# --- list operators (client-side set algebra; curated order preserved) --------
class _FakeListOps:
    LISTS = {
        10: [(3, "2003"), (1, "2001"), (2, "2002")],
        20: [(2, "2002"), (3, "2003"), (4, "2004")],
    }

    def get_list(self, lid, *, media="movie"):
        return [
            MediaItem(title=f"l{t}", tmdb_id=t, release_date=f"{d}-01-01")
            for t, d in self.LISTS[lid]
        ]


def test_list_all_intersects_keeping_curated_order():
    # lists 10 {3,1,2} ∩ 20 {2,3,4} -> {2,3} in list-10 order -> [3,2]
    # (curated, NOT release-sorted)
    r = run_builders(
        "X",
        CollectionDef(media="movie", tmdb_list={"all": [10, 20]}),
        tmdb=_FakeListOps(),
        mdblist=None,
    )
    assert [m.tmdb_id for m in r.movies] == [3, 2]


def test_list_except_subtracts():
    # list 10 {3,1,2} minus list 20 {2,3,4} -> {1}
    r = run_builders(
        "X",
        CollectionDef(media="movie", tmdb_list={"any": [10], "except": [20]}),
        tmdb=_FakeListOps(),
        mdblist=None,
    )
    assert [m.tmdb_id for m in r.movies] == [1]


# --- match: all (cross-key intersection) ----------------------------------------------
class _FakeMatch:
    def get_collection(self, cid):
        return MovieCollection(
            tmdb_id=cid,
            name="c",
            movies=[
                MediaItem(title=f"m{t}", tmdb_id=t, release_date=f"20{t:02d}-01-01")
                for t in [1, 2, 3]
            ],
        )

    def get_keyword_movies(self, with_keywords, *, without_keywords=None, limit=None):
        return [
            MediaItem(title=f"k{t}", tmdb_id=t, release_date=f"20{t:02d}-01-01")
            for t in [2, 3, 4]
        ]


def test_match_all_intersects_across_keys():
    # collection {1,2,3} AND keyword {2,3,4} -> {2,3}
    coll = CollectionDef(
        media="movie", tmdb_collection=1, tmdb_keyword=100, match="all"
    )
    r = run_builders("X", coll, tmdb=_FakeMatch(), mdblist=None)
    assert [m.tmdb_id for m in r.movies] == [2, 3]


def test_match_any_unions_by_default():
    coll = CollectionDef(
        media="movie", tmdb_collection=1, tmdb_keyword=100
    )  # default match = any
    r = run_builders("X", coll, tmdb=_FakeMatch(), mdblist=None)
    assert [m.tmdb_id for m in r.movies] == [1, 2, 3, 4]
