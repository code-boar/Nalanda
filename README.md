<div align="center">

<h1>
  <img src="images/nalanda_logo.svg" alt="Nalanda" width="400" />
</h1>

The original Nalanda was one of the ancient world's great libraries. This one is less
ambitious: it keeps your Jellyfin library tidy.

Describe the collections you want in one YAML file and Nalanda builds and maintains them as
Jellyfin BoxSets of movies and TV shows, optionally syncing with
[Radarr](https://radarr.video/) (movies) and [Sonarr](https://sonarr.tv/) (TV). It is a
simplified collection and metadata manager for Jellyfin, inspired by
[Kometa](https://kometa.wiki/).

Re-run it as your sources change, or on a schedule, and your collections follow: a "Trending
This Week" list re-trends, a franchise gains its latest film.

</div>

## Features

- **[Builders](#the-builder-model-three-levels-then-escape-hatches)**. Build a collection from TMDB, TVDB, or MDBList: whole franchises and curated
  lists, everything in a genre or tagged with a keyword, a person's cast or crew credits, a
  studio's or network's catalogue, popularity and trending charts, or a custom query of your
  own. Mix sources together, require or exclude specific attributes, and sequence the groups in
  the order you want. Names or ids both work.
- **[Movies, TV, and mixed](#tv-collections-media-tv)**. Each collection is movies, TV, or both. Every source works for
  either, and a mixed collection gathers films and series into one BoxSet (a whole franchise
  in a single place), syncing with both Radarr and Sonarr.
- **[Jellyfin collections](#collection-level-controls)**. Nalanda builds each collection as a Jellyfin BoxSet and keeps its
  membership, description, artwork, and ordering in step with your config. Artwork is pulled
  from TMDB automatically, or you can supply your own poster, thumbnail, and backdrop. Related
  collections can be grouped together in the library view.
- **[Per-item metadata](#per-item-metadata)**. Write and lock specific fields (parental rating, title, overview,
  sort title) on individual movies and shows in Jellyfin, independent of any collection.
- **[Radarr](#radarr-integration) and [Sonarr](#sonarr-integration)** *(optional)*. Hand a collection's titles to Radarr or Sonarr: add the
  ones you don't have yet, line up their quality profile and monitoring, and tag each title so a
  collection's members stay identifiable. Nalanda only ever touches its own tags, never yours.
- **[Webhook daemon and scheduling](#webhook-daemon-nalanda-serve)** *(optional)*. Run Nalanda as a background service that
  rebuilds on a schedule, or re-syncs the moment Radarr or Sonarr signals a change, touching
  only the collections affected.
- **[Metadata cache](#metadata-cache)**. A disposable on-disk cache of what TMDB, TVDB, and MDBList return, so
  repeated runs don't re-fetch the same data. Safe to delete; it never affects correctness.
- **[Editor validation](#editor-validation)**. A JSON Schema generated from the code validates `config.yml` as you
  type, so typos are caught immediately.

## Requirements

- A Jellyfin server and an API key
- To run Nalanda: Docker, or Python 3.14+ and [uv](https://docs.astral.sh/uv/) for a local
  install from source
- Optional: Radarr (movies) and Sonarr (TV) for syncing and tagging; an MDBList API key
  for `mdblist_*` builders; a TMDB API read token for any `tmdb_*` builder. TVDB needs no key,
  because Nalanda ships a per-project one, so `tvdb_*` builders work out of the box.

## Installation

Nalanda needs two files: `.env` for secrets and connection details (gitignored), and
`config.yml` for your collections. Deploy with Docker, or run it locally from source with uv.

### Docker

Images are published to the GitHub Container Registry at `ghcr.io/code-boar/nalanda`.
`latest` tracks the newest release; each release is also tagged `X.Y.Z` (plus `X.Y` and
`X`), so pin a version for reproducible deploys.

The image is rootless: it runs as a baked non-root user (uid/gid `1000`), with no
privilege-dropping entrypoint. Everything Nalanda persists lives in one mounted directory,
`/config` (holding `config.yml`, the state file, and the co-located `.env`), so the rest of the
container filesystem can be read-only. Start from the bundled compose file:

```sh
cp docker/compose.example.yml docker-compose.yml   # then edit to taste
docker compose up -d
```

**First run.** With an empty `./config`, Nalanda seeds a starter `config.yml` and `.env` into
it, then comes up idle and healthy -- it does nothing useful yet, but it does not crash-loop.
Edit the two seeded files, `./config/config.yml` (your collections) and `./config/.env` (your
Jellyfin URL and API key, plus any Radarr/Sonarr/MDBList keys), then `docker compose restart`.
Any later change to those files also needs a restart: `.env` is read once at startup, and the
config's schedules and known-collection set are wired at startup too.

**Ownership.** Because nothing in the container runs as root, it cannot fix the mount's
ownership for you. `./config` must be writable by the uid the container runs as. With the
default uid `1000`, bind-mounting a fresh host folder usually means a one-time
`chown 1000:1000 ./config`. To run as a different uid, set `user:` in the compose file (e.g.
`user: "${DOCKER_UID}:${DOCKER_GID}"`); `PUID`/`PGID` environment variables do nothing here,
as there is no entrypoint that reads them.

**Secrets.** Keep them in `./config/.env` (a file Nalanda reads), not `-e` environment
variables. A file is not exposed in `docker inspect` or the host process environment, and
Nalanda loads it into memory without re-exporting it, so the keys do not leak through the
container's own environment either. The seeded `.env` is created owner-only (`0600`); if you
supply your own, run `chmod 600 ./config/.env` to match.

**Runtime.** The image runs the daemon under `tini` as PID 1 for a clean shutdown, bundles a
healthcheck that calls its own `/health`, and includes `tzdata` so setting `TZ` makes the cron
fire on your local time.

**Networking.** If Radarr/Sonarr run in the same Docker network, point their webhooks at
`http://nalanda:8842/trigger` (no published port needed); the image sets `NALANDA_HOST=0.0.0.0`
so a published port is reachable. See [Network exposure](#network-exposure) for publishing to
the LAN -- keep the endpoint on a trusted network behind a TLS reverse proxy, since it is plain
HTTP with a header secret.

### Local (uv)

```sh
uv sync                          # install dependencies
cp .env.example .env             # add your TMDB / Jellyfin (and optional Radarr/Sonarr/MDBList) keys
cp config.example.yml config.yml # define your collections
uv run python -m nalanda schema  # optional: regenerate config.schema.json for editor validation
```

`config.example.yml` is a worked example you can copy as a starting point; the
[configuration reference](#configuration-reference) below documents every option.

## Usage

These examples use the local (uv) install. With Docker the container already runs `serve`; to
invoke any other subcommand, run it inside the container without the `uv run` prefix, e.g.
`docker compose exec nalanda python -m nalanda run collections "A Movie Franchise"`.

```sh
# Build every collection in config.yml, or just the named ones
uv run python -m nalanda run                 # run everything (collections, then metadata)
uv run python -m nalanda run collections "A Movie Franchise" "Some Favourite Shows"
uv run python -m nalanda run metadata        # apply per-item metadata overrides only

uv run python -m nalanda run --dry-run       # preview; logs every intended write, mutates nothing

# Build a single collection from one source (a TMDB collection id or MDBList URL)
uv run python -m nalanda build "A Movie Franchise" 1234

# Run the webhook daemon
uv run python -m nalanda serve

# Apply per-item metadata overrides on demand (also runs on a schedule under serve)
uv run python -m nalanda run metadata

# Inspect or maintain the metadata cache
uv run python -m nalanda cache info

# Regenerate the JSON schema after upgrading
uv run python -m nalanda schema
```

A bare `uv run python -m nalanda <tmdb-id | mdblist-url | tvdb:ref>` prints a read-only report:
what the source resolves to and its status in Jellyfin and Radarr/Sonarr. Use it to inspect a
source before adding it.

## How it works

For each collection in `config.yml` (`media: movie`, `tv`, or `mixed`):

1. **Build** — run the builders to get an ordered set of titles, each with its TMDB, TVDB,
   and IMDb ids.
2. **Match** — find which titles already exist in the Jellyfin library.
3. **Sync Radarr/Sonarr** *(when a `radarr:` or `sonarr:` block has `enable: true`)* — add any
   missing titles and reconcile the identity tag, quality profile, and monitoring on the rest.
4. **Assemble** — create or reconcile the Jellyfin BoxSet: membership, display order,
   overview, images, and sort title, writing only what differs from the server.

A small state file (`.nalanda-state.json`) records the image last applied to each slot (a URL,
or a content hash for a local file, which Jellyfin can't read back), so an image is re-applied
only when it changes.

## Configuration reference

`config.yml` defines your collections; `.env` holds secrets and connection details. Nalanda's
builders are named per-attribute keys that accept names or ids, with sensible defaults; the
`tmdb_discover` escape hatch still takes raw TMDB parameters for the rare compound query.

## Editor validation

A JSON Schema is generated from the code (`nalanda/config.py`), so editor validation can't
drift from what Nalanda accepts. The starter config already references it, and a YAML-aware
editor (for example VS Code with the Red Hat YAML extension) validates as you type:

```yaml
# yaml-language-server: $schema=./config.schema.json
```

The schema is read only by your editor; Nalanda validates the config itself when it loads,
so this is purely an authoring aid. It is generated from the version you run rather than
fetched from a URL, so it can't get ahead of the code that actually enforces it. Every
`run` and `serve` writes `config.schema.json` next to your `config.yml` and refreshes it,
so it stays matched across upgrades, in Docker and from a checkout alike. There is also a
one-off command if you want to regenerate it by hand:

```sh
uv run python -m nalanda schema   # writes ./config.schema.json
```

Unknown keys are always rejected, so typos surface immediately, and everything documented
here is live.

## The builder model: three levels, then escape hatches

There are three separate "combine" decisions, each with its own operator. Keeping them
distinct is what keeps the config readable.

|                      Level | Decision                                     | Operator                 |
| -------------------------: | -------------------------------------------- | ------------------------ |
|        1. within a **key** | how multiple values of one attribute combine | `all` / `any` / `except` |
|      2. within a **block** | how different keys combine                   | `match: all \| any`      |
| 3. within a **collection** | how separate groups are sequenced            | `append:`                |

Past these three levels, reach for an escape hatch rather than deeper nesting:

- an arbitrary server-side query: `tmdb_discover:`
- an arbitrary hand-picked set: `tmdb_movie:` / `tmdb_show:` / `tmdb_title:` lists

There is no `append` inside `append`, and no recursive `except` blocks.

## Level 1: sources and per-key operators

A *source key* produces titles. Multi-valued keys (a title has many genres, a person has many
credits) accept an `{all|any|except}` mapping; single-identity keys do not.

| Key                                                                                                                       | Value                                                   | `all`/`any` | `except` | Order              |
| ------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------- | :---------: | :------: | ------------------ |
| `tmdb_collection`                                                                                                         | id \| list                                              |      —      |    —     | chronological      |
| `tmdb_movie`                                                                                                              | id \| list                                              |      —      |    —     | chronological      |
| `tmdb_title`                                                                                                              | "Title" \| "Title (YYYY)" \| list                       |      —      |    —     | chronological      |
| `tmdb_keyword`                                                                                                            | id \| name \| list \| `{all\|any\|except}`              |     yes     |   yes    | chronological      |
| `tmdb_genre`                                                                                                              | id \| name \| list \| `{all\|any\|except}`              |     yes     |   yes    | chronological      |
| `tmdb_list`                                                                                                               | id \| list \| `{all\|any\|except}`                      |     yes     |   yes    | curated            |
| `mdblist_list`                                                                                                            | url/ref \| list \| `{url\|all\|except, sort_by, limit}` |    yes¹     |   yes    | server (`sort_by`) |
| `mdblist_catalog`                                                                                                         | `{sort, score_min, genre, year_min, …, limit}`          |     n/a     |   n/a    | `sort`             |
| `mdblist_official`                                                                                                        | slug \| `{slug, sort_by, limit}`                        |      —      |    —     | server             |
| `tmdb_company`                                                                                                            | id \| name \| list \| `{all\|any\|except}`              |     yes     |   yes    | chronological      |
| `tmdb_actor` / `tmdb_director` / `tmdb_writer` / `tmdb_producer` / `tmdb_crew`                                            | id \| name \| list \| `{all\|any\|except}`              |     yes     |   yes    | chronological      |
| `tmdb_popular` / `tmdb_trending_daily` / `tmdb_trending_weekly` / `tmdb_top_rated` / `tmdb_upcoming` / `tmdb_now_playing` | **count** (int)                                         |      —      |    —     | chart order        |
| `tmdb_discover`                                                                                                           | raw Discover dict (escape hatch)                        |     n/a     |   n/a    | `sort_by`          |

¹ `mdblist_list` uses `url` (union) rather than `any`; `all` intersects lists, `except`
subtracts. MDBList items are enriched (genres mapped to TMDB ids, so `tmdb_genre: {except}`
and `match: all` work on them; plus full release dates and cross-source `ratings`).
`mdblist_list` sorts and limits server-side with a rich vocabulary
(`imdbrating` / `rtomatoes` / `metacritic` / `rank` / …); `mdblist_catalog` filters and sorts
MDBList's whole database by those same cross-source ratings.

Names and ids are interchangeable everywhere a name is accepted, and may be mixed
(`all: [28, Adventure]`). Names resolve against TMDB (the genre list, or keyword / person /
company search). Genre names are case- and punctuation-insensitive but must use TMDB's
canonical spelling (`Science Fiction`, not `Sci-Fi`). Person and company names resolve to the
most-popular match (for companies, the studio with the most titles); use an id to
disambiguate. People builders use credits: `tmdb_director` / `writer` / `producer` filter crew
by department. A chart's value is the count of titles to take, for example `tmdb_popular: 30`.

**Ordering within a block.** A sole curated source (exactly one `tmdb_list`, `mdblist_list`,
chart, or `tmdb_discover`, and nothing chronological) keeps its server order. Any other
combination is unified and sorted by release date, so mixed sources interleave
chronologically. This same distinction sets the default `order:` when you don't specify one:
a release-sorted block defaults to `release_date`, a sole curated source to `source`.

```yaml
# single source
Westerns:            { tmdb_genre: Western }
Films by a Director: { tmdb_director: A Director Name }
From One Studio:     { tmdb_company: A Studio Name }

# per-key value combine
Action-Adventure:
  tmdb_genre: { all: [Action, Adventure] }            # films that are BOTH (AND)
Either Actor:
  tmdb_actor: [First Actor, Second Actor]             # a list = union of both actors' films
Both Actors:
  tmdb_actor: { all: [First Actor, Second Actor] }    # intersection: films with both

# per-key exclude (a filter, not a source; can stand alone)
A Keyword, minus the making-of docs:
  tmdb_keyword: 1234
  tmdb_genre: { except: [Documentary] }
```

## Level 2: combining keys (`match`)

By default, multiple keys in one block are unioned (`match: any`). Set `match: all` to require
every key, the compound intersection case (release-sorted).

```yaml
Sci-Fi with an Actor:
  match: all
  tmdb_genre: Science Fiction
  tmdb_actor: An Actor Name
  limit: 300          # cap the broad genre fetch (intersect their films with the top 300)
```

For compound queries using parameters Nalanda doesn't wrap, drop to the escape hatch:

```yaml
Highest-Grossing R Comedies:
  tmdb_discover: { with_genres: 1234, certification: R, sort_by: revenue.desc }
```

## Level 3: ordered composition (`append`)

Each block is resolved independently (levels 1–2), then blocks are concatenated in the order
listed, so you control cross-source ordering.

```yaml
Saga Marathon:
  tmdb_list: 1234             # block 0: curated order
  append:
    - tmdb_collection: 1234   # block 1: chronological
    - tmdb_title: [Example Film]  # block 2
```

## Collection-level controls

These apply to the whole collection, orthogonal to the builders above.

| Key                                      | Description                                                                                                                                                                                                                                           |
| ---------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `media`                                  | **Required** (no default). `movie` \| `tv` \| `mixed`. Selects the Jellyfin library type(s), builder dialect, and `arr`(s). `mixed` = one BoxSet of movies and shows; see [Mixed collections](#mixed-collections-media-mixed).                        |
| `overview`                               | Overview text; overrides any source metadata.                                                                                                                                                                                                         |
| `tmdb_overview`                          | A TMDB collection id whose overview is fetched at runtime (useful for merges, which have no sole source to auto-fill from). A literal `overview` overrides it.                                                                                        |
| `primary_art_url` / `primary_art_file`   | Primary (poster) image: a remote URL Jellyfin downloads, or a path to a local file whose bytes are uploaded. Overrides the artwork repo and the auto-sourced TMDB poster. The two are mutually exclusive.                                             |
| `thumb_art_url` / `thumb_art_file`       | Thumb (landscape title card) image: URL or local file. Overrides the artwork repo and the auto-sourced TMDB *titled* backdrop. Mutually exclusive.                                                                                                    |
| `backdrop_art_url` / `backdrop_art_file` | Backdrop (fanart) image: URL or local file. Overrides the artwork repo and the auto-sourced TMDB *textless* backdrop. Mutually exclusive.                                                                                                             |
| `sort_title`                             | Explicit sort title for positioning the collection itself (see [Sections](#sections)).                                                                                                                                                                |
| `section`                                | Name of a top-level `sections` entry (see [Sections](#sections)).                                                                                                                                                                                     |
| `order`                                  | `source` (as built) \| `sort_name` \| `release_date`. Unset = a smart default: `release_date` when the collection is release-sorted at build (single/merged collections, query builders), else `source` (a sole curated list keeps its server order). |
| `sync_mode`                              | `append` (add only) \| `sync` (also remove). Overrides the global default.                                                                                                                                                                            |
| `hide_year`                              | Empty the collection's year and release-date fields. Overrides the global `hide_year` (default `true`).                                                                                                                                               |
| `run_schedule`                           | Per-collection cron override for `nalanda serve` (level 3 of the cascade): a name from `settings.run_schedules`, an inline cron, or `none` / `disabled` to opt out. See [Scheduling](#scheduling).                                                    |
| `libraries`                              | Jellyfin libraries (by name) to match within; omit for all libraries of the collection's media type.                                                                                                                                                  |
| `limit`                                  | Cap each broad query builder (genre/keyword/company/people/discover) most-popular-first. Defaults to 100; set `0` for unlimited.                                                                                                                      |
| `radarr`                                 | Radarr sync and identity-tag options (see [Radarr integration](#radarr-integration)). For `movie` or `mixed` collections; set `enable: true` to opt in; fields inherit `settings.radarr`.                                                             |
| `sonarr`                                 | Sonarr sync and identity-tag options (see [Sonarr integration](#sonarr-integration)). For `tv` or `mixed` collections; inherits `settings.sonarr`.                                                                                                    |

Global defaults live under `settings:`: `sync_mode`; `language` / `region` (TMDB text metadata
and release dates, see [Language & region](#language--region)); `hide_year` (default `true`,
empties every collection's year/release-date fields, per-collection overridable);
`delete_unconfigured_collections` (default `false`: on a full run, delete collections Nalanda
previously made that are no longer in the config, never touching BoxSets it didn't create);
`unlock_unconfigured_metadata` (default `false`: when a `metadata:` entry or field is removed
from config, unlock it in Jellyfin so a refresh can reclaim it; see
[Per-item metadata](#per-item-metadata));
`radarr` / `sonarr` (default options inherited by every `radarr:` / `sonarr:` block);
`webhook` (the inbound-request behaviour of the [serve daemon](#webhook-daemon-nalanda-serve));
and `run_schedules` / `run_schedule` / `jobs` (the cron [Scheduling](#scheduling) cascade the
daemon runs on; `jobs.metadata` sets the per-run-kind default for the metadata job).

## Language & region

`language` and `region` are two different axes, a common point of confusion because a locale
like `en-GB` already contains a region:

| Setting    | Format                                                                                            | What it controls                                                                                                                                                                      |
| ---------- | ------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `language` | TMDB language tag: ISO 639-1, optionally with a region (`en`, `en-US`, `en-GB`, `fr-FR`, `pt-BR`) | The language of TMDB *text* metadata: titles, overviews, genre names. The region suffix only nudges regional text variants (`pt-BR` vs `pt-PT`; US vs GB spellings). Default `en-US`. |
| `region`   | ISO 3166-1 (`US`, `GB`)                                                                           | Release dates only, on chart/discover builders. Independent of `language`'s region suffix.                                                                                            |

Put the language you want your overviews and titles in as `language` (most people leave it at
`en-US`, or use `en-GB` / `en-AU` for local spellings). Set `region` only if you care about
region-correct release dates on charts and discover.

Two things to know:

- `language` does not affect artwork. Image selection uses only the language subtag (`en` out
  of `en-GB`) plus textless art, so `en-GB` vs `en-US` changes the text but never which
  poster or backdrop is chosen.
- TVDB metadata is always English today, regardless of `language`: `tvdb_*` builders resolve
  under a fixed `eng` / `usa`. So `language` currently reaches TMDB only.

## Sections

Jellyfin sorts collections (BoxSets) by name; the only lever for custom ordering is a
collection's sort title. Setting one by hand on every collection is tedious, so Nalanda adds
**sections**: a top-level, ordered list of section names. A collection's `section` gives it an
auto-generated sort title prefixed by the section's position, so sections group together in
that order in the library view.

```yaml
sections:                  # the order here is the order sections appear in Jellyfin
  - Charts
  - Universes
  - Studios

collections:
  Top 100:         { section: Charts,    tmdb_list: 1234 }        # → sort title "001 …"
  Movie Franchise: { section: Universes, tmdb_collection: 1234 }  # → "002 movie franchise"
  From One Studio: { section: Studios,   tmdb_company: A Studio Name } # → "003 from one studio"
```

How the sort title is derived (the `sort_title` key composes with `section`):

| `section` | `sort_title` | Resulting sort title                                                     |
| :-------: | :----------: | ------------------------------------------------------------------------ |
|     —     |      —       | *none (Jellyfin's native name sort)*                                     |
|     —     |     set      | `sort_title` verbatim (manual override)                                  |
|    set    |      —       | `<NNN> <normalized name>`                                                |
|    set    |     set      | `<NNN> <sort_title>` (section grouping plus custom within-section order) |

The normalized name replicates Jellyfin's own sort-name rules (ported from
`BaseItem.CreateSortName`, read live from `/System/Configuration`): lower-cased, with the
article words (`the` / `a` / `an` by default) stripped at the start, middle, and end, and the
configured punctuation removed or replaced. So "The Best of the Year" becomes
`best of year`, matching how Jellyfin sorts everything else.

- Nalanda sets the BoxSet's `ForcedSortName`. Jellyfin zero-pads digit runs internally, so the
  stored value `001 movie franchise` sorts as `0000000001 movie franchise`.
- Within a section, collections sort alphabetically by default; add a short `sort_title`
  (`"01"`, `"02"`) to force a custom order.
- Collections without a `section` keep Jellyfin's native sort and fall after the sectioned
  ones (numbers sort before letters).
- A collection's `section` must be a declared `sections` entry, or the config is rejected.

## Metadata ownership

Nalanda owns every collection's overview and images. It sets them from the data it pulled that
run, locks `Name` / `Overview` (and sets `LockData`) so Jellyfin's own provider can't
overwrite them, and reconciles by comparison, writing only when the desired value differs from
what's on the server (images are compared against a saved marker, since Jellyfin doesn't
expose the image source). Nothing is delegated to Jellyfin's fetch.

What differs between collections is where the *defaults* come from, and whether the TMDB id is
stamped:

- A collection that is exactly one `tmdb_collection` (no merge, no list) has that collection
  id stamped as its provider id, and its overview and images default to that collection's TMDB
  metadata, so you needn't supply them. An `overview` / `tmdb_overview` or any per-slot art
  override still wins.
- A merge, list, keyword, or other source has no sole source to draw from, so its provider id
  is left blank (`{}`) and its overview and images default to empty. Give it an `overview`
  (literal text) or `tmdb_overview` (a collection id whose overview is fetched at runtime), and
  per-slot art (URL, local file, or an [artwork repo](#artwork-repository) file).

### Images (Primary / Thumb / Backdrop)

The three Jellyfin image slots map to TMDB the way Jellyfin's own provider does: a backdrop
*with* a language tag (it has title text) is the Thumb, a *textless* backdrop is the Backdrop,
and a poster is the Primary. For a single `tmdb_collection` all three are auto-sourced; among
candidates the preferred metadata language (`settings.language`) wins, then English, then
community rating, matching what Jellyfin would download. Any slot Nalanda doesn't set is
cleared, so a collection shows only its own images.

Each slot is resolved independently, in this priority order:

1. an explicit per-slot override: `<slot>_art_url` (a remote URL Jellyfin downloads) or
   `<slot>_art_file` (a path to a local file whose bytes Nalanda uploads);
2. a matching file in the [artwork repository](#artwork-repository);
3. the auto-sourced TMDB image;
4. nothing (the slot is left clear).

`<slot>_art_url` and `<slot>_art_file` are mutually exclusive; set at most one per slot
(setting both is a config error). If an explicit override is invalid (a malformed URL, or a
file that doesn't exist), Nalanda logs a warning and falls through to the repo, then TMDB; a
simply-absent override is silent. Change detection works for both kinds: a URL is compared by
its string, a local file by a hash of its bytes, so editing a file (even under the same path)
re-uploads it, while an unchanged run leaves it alone.

### Artwork repository

Instead of (or alongside) per-collection keys, you can drop image files into a local artwork
repository and Nalanda picks them up automatically. Point it at a root, and for each
collection it looks for:

```
<path>/collections/<slug>/<type>.<ext>
```

- `<type>` is `primary`, `thumb`, or `backdrop` (the same vocabulary as the keys).
- `<ext>` is `png`, `jpg`, `jpeg`, or `webp`. If several exist for one slot, precedence is
  `png` > `jpg`/`jpeg` > `webp`, and the losers are ignored with a warning.
- `<slug>` is the collection name slugified: lowercased, with runs of non-alphanumeric
  characters collapsed to a single `-`. So `"A Title: Subtitle"` → `a-title-subtitle`.
  This is the same slug Nalanda uses for Radarr/Sonarr identity tags, so a collection has one
  identity on disk and in the `*arr`s. When an artwork repo is configured, two collection
  names that slugify to the same folder are a config error.

Configure it under a top-level `artwork_repo:` block:

```yaml
artwork_repo:
  path: /artwork                  # repo root; images live under <path>/collections/<slug>/
  create_empty_folders: false     # create an empty folder per collection to drop art into
  delete_old_empty_folders: false # on a full run, remove empty folders no longer configured
```

`create_empty_folders` makes the drop-folders for you, so you never have to guess the slug.
`delete_old_empty_folders` tidies up on a full `run`: it removes a `collections/<slug>/` folder
only when its collection is no longer configured and the folder is empty. A folder containing
any file is never touched.

## Radarr integration

Add a `radarr:` block with `enable: true` to a movie (or mixed) collection to manage it in
Radarr. `enable` defaults to `false`, so a block without it is inert: configuration is kept but
nothing is added, tagged, or searched (handy as an off switch). Every other field is optional
and inherits `settings.radarr`; `radarr: { enable: true }` opts in on all defaults.

```yaml
collections:
  A Synced Movie List:
    media: movie
    mdblist_list: https://mdblist.com/lists/someuser/some-list
    sync_mode: sync
    radarr:
      enable: true               # opt in (omit or false = inert)
      add_missing: true          # add movies not yet in Radarr (tagged on add)
      add_existing: true         # tag movies already in Radarr
      quality_profile: HD-1080p  # name or id (required when adding/upgrading)
      root_folder: /movies
      search: true               # search for newly added movies only
```

Nalanda owns one identity tag per collection, `<tag_prefix><slug>` (default `nalanda-<slug>`,
for example `nalanda-movie-franchise`), and keeps exactly the desired members tagged. The
`tag_prefix` is a namespace: Nalanda only ever adds or removes tags inside it, never touching
manual or other-tool tags. This tag is also the routing key for the
[webhook](#webhook-daemon-nalanda-serve).

| Key                    |   Default   | Description                                                                             |
| ---------------------- | :---------: | --------------------------------------------------------------------------------------- |
| `enable`               |   `false`   | Opt-in switch. `true` = manage this collection in Radarr; `false` or omitted = inert.   |
| `add_missing`          |   `false`   | Add collection movies not yet in Radarr (tagged on add).                                |
| `add_existing`         |   `false`   | Apply the identity tag to movies already in Radarr.                                     |
| `upgrade_existing`     |   `false`   | Reset existing members' quality profile to `quality_profile` (diff-first).              |
| `monitor_existing`     |   `false`   | Reset existing members' monitored flag to `monitored`.                                  |
| `monitored`            |   `true`    | Monitor newly added movies.                                                             |
| `search`               |   `false`   | Search for newly added movies (never existing ones).                                    |
| `minimum_availability` | `released`  | `announced` \| `in_cinemas` \| `released`.                                              |
| `quality_profile`      |      —      | Profile name or id. Required when `add_missing` or `upgrade_existing`.                  |
| `root_folder`          |      —      | Root folder path. Required when `add_missing`.                                          |
| `tag`                  | *name slug* | Override the identity-tag slug (final label = `tag_prefix` + this).                     |
| `tag_prefix`           | `nalanda-`  | Namespace prefix for identity tags.                                                     |
| `stale_tags`           |   `mark`    | When a movie leaves: `mark` (→ `nalanda-x-stale`) \| `delete` (drop the tag) \| `keep`. |
| `stale_suffix`         |  `-stale`   | Suffix for the `mark` policy.                                                           |

Tagging reconciles to the desired set each run (a bulk `PUT /movie/editor`) and is
decoupled from search, so "add and tag but don't search" is a valid configuration. A movie
that leaves a collection is marked, deleted, or kept per `stale_tags`; a re-joining movie has
its stale tag cleared and the live tag restored. Quality-profile and monitored changes to
existing members are opt-in (`upgrade_existing` / `monitor_existing`) and diff-first. Nothing
is added unless you ask. Preview the whole pipeline with `--dry-run`: every intended write
is logged, nothing mutates.

## TV collections (`media: tv`)

Every collection must declare `media`: `movie`, `tv`, or
[`mixed`](#mixed-collections-media-mixed), with no default. The setting selects the Jellyfin
library type (movie vs show), the builder dialect, and the `arr` (Radarr vs Sonarr). A TV
BoxSet holds whole shows, exactly as a movie BoxSet holds whole movies; there are no episode-
or season-level collections.

All three sources are media-dual: most builder keys work for both and dispatch on `media` (for
example, `tmdb_genre` hits movie Discover for a movie collection, TV Discover for a tv one).
Only a few keys are entity-specific:

|             | Movie-only                                                           | TV-only                                                             | Shared (dispatched by `media`)                                                                                                                                                                                 |
| ----------- | -------------------------------------------------------------------- | ------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **TMDB**    | `tmdb_collection`, `tmdb_movie`, `tmdb_now_playing`, `tmdb_upcoming` | `tmdb_show`, `tmdb_network`, `tmdb_on_the_air`, `tmdb_airing_today` | `tmdb_title`, `tmdb_genre`, `tmdb_keyword`, `tmdb_actor`/`director`/`writer`/`producer`/`crew`, `tmdb_company`, `tmdb_discover`, `tmdb_list`, `tmdb_popular`, `tmdb_top_rated`, `tmdb_trending_daily`/`weekly` |
| **TVDB**    | `tvdb_movie`                                                         | `tvdb_show`                                                         | `tvdb_list`, `tvdb_discover`                                                                                                                                                                                   |
| **MDBList** | —                                                                    | —                                                                   | `mdblist_list`, `mdblist_catalog`, `mdblist_official`                                                                                                                                                          |

Using a movie-only key on a `tv` collection (or vice versa) is rejected by the schema; a
[`mixed`](#mixed-collections-media-mixed) collection accepts both families. The TV-specific
keys mirror their movie analogues: `tmdb_show` ≈ `tmdb_movie` (show ids), `tmdb_network` ≈
`tmdb_company` (TV networks, by id, since TMDB has no network name search), and
`tmdb_on_the_air` / `tmdb_airing_today` are the TV charts movies lack.

> **Matching note.** Shows from the shared TMDB builders (the charts, `tmdb_discover`,
> `tmdb_genre` / `tmdb_keyword`, people) carry only a TMDB id, whereas `tmdb_show` (by id) and
> the `tvdb_*` builders also resolve a TVDB id. Shows are matched TVDB-first, then TMDB, then
> IMDb, so a chart- or discover-sourced show matches a Jellyfin (or Sonarr) entry only if that
> entry exposes a TMDB id. If your TV library is indexed by TheTVDB alone, prefer the `tvdb_*`
> builders (or id-based `tmdb_show`) for those collections, so every show carries a TVDB id to
> match on.

### TVDB builders (`tvdb_*`)

TVDB ids are Sonarr-native, and TVDB tracks movies too (so `tvdb_movie` / `tvdb_list` feed
movie collections, resolved to TMDB/IMDb ids for Radarr/Jellyfin). `tvdb_show` / `tvdb_movie`
accept id(s), slug(s), or name(s); `tvdb_list` takes a list id or slug (curated order);
`tvdb_discover` is a raw filter escape hatch
(`genre` / `company` / `country` / `lang` / `year` / `sort`, with `country` + `lang` defaulting
to `usa` / `eng`).

No API key is needed. TVDB v4 keys are per-project, not per-user (per TVDB's docs, "individual
users shouldn't need an API key"), and limits are per-IP, so Nalanda ships its own. The
`tvdb_*` builders work out of the box, and TVDB is contacted only when one runs. (Maintainers
rotating the bundled key: re-run `scripts/encode_tvdb_key.py` and push the new strands.)

> Metadata provided by [TheTVDB](https://thetvdb.com/). Please consider adding missing
> information or [subscribing](https://thetvdb.com/subscribe). (Nalanda also logs this once per
> run whenever a `tvdb_*` builder is used; see [Attribution](#attribution).)

## Sonarr integration

Add a `sonarr:` block with `enable: true` to a `tv` or `mixed` collection to manage it in
Sonarr, the TV analogue of [`radarr:`](#radarr-integration), with the same identity-tag design,
three-state stale policy, and `enable` off-switch. Shows are matched
tvdb-first, then tmdb/imdb. Set `SONARR_URL` / `SONARR_API_KEY` in `.env`.

| Key                                                  |   Default   | Description                                                                           |
| ---------------------------------------------------- | :---------: | ------------------------------------------------------------------------------------- |
| `enable`                                             |   `false`   | Opt-in switch. `true` = manage this collection in Sonarr; `false` or omitted = inert. |
| `add_missing`                                        |   `false`   | Add collection shows not yet in Sonarr (tagged on add).                               |
| `add_existing`                                       |   `false`   | Apply the identity tag to shows already in Sonarr.                                    |
| `upgrade_existing`                                   |   `false`   | Reset existing members' quality profile to `quality_profile` (diff-first).            |
| `monitor_existing`                                   |   `false`   | Reconcile existing members' `monitored` flag and season monitoring (see below).       |
| `monitored`                                          |   `true`    | Series-level monitored flag for added shows.                                          |
| `monitor`                                            |    `all`    | Episode/season strategy (see below).                                                  |
| `search`                                             |   `false`   | Search for missing episodes when adding (never existing).                             |
| `cutoff_search`                                      |   `false`   | Also search cutoff-unmet episodes on add.                                             |
| `series_type`                                        | `standard`  | `standard` \| `daily` \| `anime` (episode numbering).                                 |
| `season_folder`                                      |   `true`    | Organise episodes into season folders.                                                |
| `quality_profile`                                    |      —      | Profile name or id. Required when `add_missing` or `upgrade_existing`.                |
| `language_profile`                                   |      —      | Sonarr v3 only (v4 dropped language profiles; ignored there).                         |
| `root_folder`                                        |      —      | Root folder path. Required when `add_missing`.                                        |
| `tag` / `tag_prefix` / `stale_tags` / `stale_suffix` | *as Radarr* | Identity-tag controls, identical to Radarr.                                           |

The `monitor` strategy is the one axis movies lack: which episodes and seasons a show monitors
(not which shows are in the collection). Values: `all` (every non-special season), `future`,
`missing`, `existing`, `pilot`, `first_season`, `latest_season`, `none`. It is applied at add
time via Sonarr's `addOptions.monitor`. With `monitor_existing`, the deterministic strategies
(`all` / `none` / `first_season` / `latest_season`) are reconciled each run: the
desired per-season `monitored` set is diffed against Sonarr and written (via
`PUT /series/{id}`) only on a difference. The dynamic strategies (`future` / `missing` /
`existing` / `pilot`) are state-dependent, so Sonarr owns them after add, and
`monitor_existing` then reconciles only the series-level `monitored` flag.

## Mixed collections (`media: mixed`)

A `mixed` collection builds one BoxSet holding both movies and shows, for example a franchise
with both films and series. Jellyfin BoxSets are media-agnostic (a BoxSet
links whole movies and whole series alike), so this is one collection, not two.

Media-specific keys produce their own media; shared keys produce both:

- `tmdb_collection` / `tmdb_movie` / `tvdb_movie` and the movie charts → movies
- `tmdb_show` / `tmdb_network` / `tvdb_show` and the TV charts → shows
- every shared key (`tmdb_genre`, `tmdb_keyword`, the people keys, `tmdb_company`,
  `tmdb_discover`, `tmdb_list`, the `popular` / `top_rated` / `trending` charts, and all
  `mdblist_*`) → both movies and shows

Both key families are allowed in one block; the per-media validation that applies to
`movie`/`tv` collections is relaxed for `mixed`. Members are merged and release-date sorted;
use `order:` or `append:` blocks to control ordering as usual.

```yaml
A Cross-Media Franchise:
  media: mixed
  tmdb_collection: 1234          # the films (movie endpoint)
  tmdb_show: [1234, 5678]        # the series (tv endpoint)
  overview: A franchise spanning films and series in one BoxSet.

Action Everything:
  media: mixed
  tmdb_genre: Action             # every Action movie and show
  limit: 50                      # caps EACH media (up to 50 movies and 50 shows)
```

- `limit` applies per media: a shared key capped at `limit` yields up to `limit` movies and
  `limit` shows.
- `libraries` names are matched against both pools: a movie matches within a named movie
  library, a show within a named show library (a name in neither is an error).
- A mixed collection may carry both a `radarr:` block (for its movies) and a `sonarr:` block
  (for its shows), each opted in with its own `enable: true`. Each syncs only the items of its
  media and gets its own identity tag, so either a Radarr `movie.tags` or a Sonarr
  `series.tags` webhook scopes a run to the collection.

## Per-item metadata

The `metadata:` block lets you write and lock specific fields on individual movies and shows
in Jellyfin, regardless of which collections they belong to. Entries are grouped by media
under `metadata.movies` and `metadata.shows`.

```yaml
metadata:
  movies:
    - tmdb: 1234
      parental_rating: GB-15
      sort_title: A Film 2
  shows:
    - tvdb: 5678
      parental_rating: GB-12
      title: Custom Show Title
```

### Entry shape

Each entry identifies one item with exactly one provider id, then sets at least one override
field. Movies are normally identified by `tmdb` or `imdb`; shows by `tvdb`, `tmdb`, or `imdb`.

| Field            | Jellyfin UI label  | Notes                                                                                               |
| ---------------- | ------------------ | --------------------------------------------------------------------------------------------------- |
| `parental_rating`| Parental Rating    | Written and locked (e.g. `GB-15`, `PG-13`).                                                        |
| `title`          | Title              | Written and locked.                                                                                 |
| `overview`       | Overview           | Written and locked.                                                                                 |
| `sort_title`     | Sort Title         | Written; see caveat below.                                                                          |

A bare number like `15` also works as a `parental_rating` (common for UK and Australian ratings);
it is stored as a string.

### Locking

Each supported field is written to Jellyfin and individually locked, so a normal metadata
refresh cannot overwrite it. A second run with unchanged inputs and Jellyfin state writes
nothing (compare-then-write).

### `sort_title` caveat

Jellyfin stores sort titles via `ForcedSortName`, which survives normal library refreshes and
Nalanda re-asserts the value each run. However, a "replace all metadata" refresh in Jellyfin
can clear it; the value is restored on the next metadata run.

A separate caveat: Jellyfin's API does not return `ForcedSortName`, so writing any managed
field to an item resets a sort title that was set manually in Jellyfin but is not declared in
the config. To preserve a manually-set sort title, declare it under `sort_title` in the entry.

### Removing entries: `settings.unlock_unconfigured_metadata`

By default, removing a metadata entry or field from config leaves the field locked in Jellyfin
(matching the `delete_unconfigured_collections` default). Set
`settings.unlock_unconfigured_metadata: true` to instead unlock the field (drop the field lock;
clear a managed sort title) when an entry or field is removed, so a Jellyfin refresh can
reclaim it.

### Scheduling and running metadata

The metadata job runs on `settings.jobs.metadata` (a name from `settings.run_schedules`, an
inline cron, or `none`/`disabled`), falling back to `settings.run_schedule`. Run it on demand:

```sh
uv run python -m nalanda run metadata           # apply all metadata overrides
uv run python -m nalanda run metadata --dry-run # preview without writing
```

The metadata state is tracked in `.nalanda-metadata-state.json` (a sibling of `config.yml`),
separate from the collection state file.

## Webhook daemon (`nalanda serve`)

`nalanda serve` runs a long-lived daemon (stdlib HTTP, no extra services) so Radarr, Sonarr,
or anything else can push a re-sync instead of waiting on a cron. The webhook routes are
optional: set `WEBHOOK_SECRET` in `.env` to enable them, or leave it blank to run on schedules
only -- the daemon still serves `/health` and fires cron runs, and the POST routes return 503.
The secret gates inbound HTTP only, never the internal scheduler.
There are two POST routes, split by blast radius, plus a health check. When a secret is set,
both POST routes require it in the `X-Nalanda-Token` header (compared in constant time):

| Route           | Auth  | Behaviour                                                                                                                                                                                                                                                                                      |
| --------------- | :---: | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /trigger` | token | Reactive, always scoped. `nalanda-` identity tags in a Radarr `movie.tags` / Sonarr `series.tags` payload (or a top-level `tags`) run just those collections. An explicit `{"collections": [...]}` also works. Empty, unknown, tag-less, or `Test` payloads return 204. Never runs everything. |
| `POST /run`     | token | Explicit full run, gated by `webhook.allow_full_run` (default `false`, otherwise 403).                                                                                                                                                                                                         |
| `GET /health`   | none  | Liveness, no side effects.                                                                                                                                                                                                                                                                     |

A debounce window (`webhook.debounce_seconds`, default 300) coalesces a burst of triggers into
one run; the window resets on each new trigger, so a flood of upgrades becomes a single
re-sync once it goes quiet (Jellyfin needs a moment to finish processing an upgraded file). A
single run-lock keeps runs from overlapping. A manual `nalanda run` still works while the
daemon is up: the two coordinate through a cross-process lock file next to the state file, so a
contended run waits (logging that it is queued) and then proceeds. Dry-runs skip the lock,
since they write nothing. Scheduled cron runs (see [Scheduling](#scheduling)) and webhooks
share the same lock and debounce, so they serialize and coalesce too: within a job kind a
pending full run supersedes pending scoped names, and distinct kinds never merge.

`settings.webhook` holds inbound-request behaviour only: `debounce_seconds` (`300`),
`max_wait_seconds` (an optional cap on the debounce, default none), and `allow_full_run`
(`false`). Time-driven scheduling lives in `settings.run_schedules` / `settings.run_schedule` /
`settings.jobs` (see [Scheduling](#scheduling)). The daemon's deployment knobs (config-file
path, bind address, port) come from the environment or `.env`: `NALANDA_CONFIG`
(default `./config.yml`), `NALANDA_HOST` (default `127.0.0.1`), and `NALANDA_PORT` (`8842`),
with a real environment variable overriding the `.env` value. The state and cache files
(`.nalanda-state.json`, `.nalanda-cache.db`) are written beside `config.yml`, so pointing
`NALANDA_CONFIG` at a mounted volume keeps both on it. The default host is loopback so a bare
`serve` isn't network-exposed; to publish it, set `NALANDA_HOST=0.0.0.0` (the container does)
behind a TLS-terminating reverse proxy, since the `X-Nalanda-Token` secret travels in clear
over HTTP.

> Why two files? `.env` is per-install launch config: secrets, the service URLs Nalanda
> connects to, and the `NALANDA_*` deployment knobs. It is never committed, and Nalanda reads it
> from the same directory as `config.yml`. `config.yml` is the description of what to build and
> how Nalanda behaves (collections, sync, ordering, the webhook behaviour block). Connection and
> binding details go in `.env`; logic goes in `config.yml`.

### Connecting Radarr and Sonarr

In Radarr or Sonarr, go to **Settings -> Connect -> + -> Webhook** and fill in, top to bottom:

- **Triggers** -- select the appropriate events.
- **Tags** -- optional; leave blank to send every event, or list specific `nalanda-<slug>`
  identity tags to fire only for those collections.
- **Webhook URL** -- `http://<nalanda-host>:8842/trigger` (for example
  `http://nalanda:8842/trigger` when both run on the same Docker network; see
  [Network exposure](#network-exposure) for the right address in your setup).
- **Method** -- `POST`. `PUT` is not accepted.
- **Username** -- leave blank.
- **Password** -- leave blank. Nalanda doesn't use HTTP basic auth; the shared secret travels
  in a header instead.
- **Headers** -- add one: key `X-Nalanda-Token`, value your `WEBHOOK_SECRET`. Required --
  without a matching token the request is rejected (401), and if no `WEBHOOK_SECRET` is set the
  routes are off entirely (503).

Use **Test** to confirm wiring: with a valid token it returns 204 (a success no-op -- the test
payload carries no tags), a wrong or missing token returns 401, and no secret set returns 503.

### Metadata cache

Nalanda caches what the metadata sources (TMDB, TVDB, MDBList) return, so repeated runs don't
re-fetch near-identical data; the daemon fires one per webhook and cron tick. It is a
best-effort, disposable cache: a missing, stale, or corrupt cache only ever costs speed, never
correctness. Jellyfin and Radarr/Sonarr *library* state is never cached: that is the live state
Nalanda reconciles against. The Radarr/Sonarr add-lookup metadata is cached behind the scenes
(a source fact, not library state).

`settings.cache` has an `enabled` switch (default `true`) and four duration knobs, each setting
how long a group of source data is kept:

| Knob | Default | Covers |
|------|---------|--------|
| `record_cache_duration` | `30d` | per-id details (movie / show / collection) + name/id resolution |
| `list_cache_duration`   | `1d`  | MDBList / TVDB / TMDB list membership |
| `query_cache_duration`  | `3d`  | discover / genre / company / keyword / person queries |
| `chart_cache_duration`  | `1d`  | popular / trending / time-windowed charts |

Durations are an integer with a `d` (days) or `h` (hours) suffix -- `30d`, `6h` -- or `0`
(or `off`) to bypass that bucket. Minutes are rejected (an hour is the finest useful TTL).

The cache file (`.nalanda-cache.db`, SQLite) lives beside `config.yml` and is safe to delete.
`run --refresh-cache` forces a single run to re-fetch everything and update the cache; `nalanda cache info | prune | clear
[namespace]` inspects and maintains it. The bare-source report and the one-off `build` always
read fresh.

### Scheduling

The daemon fires runs on cron schedules. A schedule resolves through a three-level cascade,
most specific first:

1. a collection's own `run_schedule:` (level 3);
2. the per-job-kind default `settings.jobs.<kind>` (level 2; `collections` and `metadata`);
3. the global default `settings.run_schedule` (level 1).

Each value is a name defined in `settings.run_schedules`, an inline cron string, or `none` /
`disabled` to opt out. Anything that resolves to nothing simply isn't scheduled; it still runs
on demand via a webhook or a manual `run`. `settings.run_schedules` is an optional map of
reusable names; any reference may be an inline cron instead. Every value is validated at load,
so a bad cron or a dangling name fails fast. Only `nalanda serve` acts on schedules; a one-off
`nalanda run` ignores them.

```yaml
settings:
  run_schedules:        # optional reusable names
    hourly: "0 * * * *"
    daily:  "0 4 * * *"
  run_schedule: daily   # global default (level 1)
  jobs:
    collections: daily  # per-job-kind default for the collection job (level 2)
    metadata: daily     # per-job-kind default for the metadata job (level 2)

collections:
  A Shared Universe:
    run_schedule: hourly  # per-collection override (level 3)
  Some Static List:
    run_schedule: none    # never scheduled (webhook or manual only)
```

Scope and pruning. Each distinct cron gets its own timer. A per-collection schedule fires a
scoped run of just that collection; the global or job-default cron fires the run that also
prunes, deleting collections and empty artwork folders no longer in config (the maintenance
pass that `delete_unconfigured_collections` gates). Because pruning rides the default cron,
giving every collection its own schedule with no global or job default means nothing prunes on
a schedule. The daemon warns at startup, and you'd prune via a manual `run` or `POST /run`.

### Network exposure

Two independent layers decide who can reach the daemon:

1. The bind (`NALANDA_HOST`, set in the environment or `.env`) is which interfaces the process
   listens on. Inside a container, `0.0.0.0` means all interfaces *in the container's network
   namespace*, not your physical host, so on its own it exposes nothing beyond the Docker
   network. (`127.0.0.1` inside a container is nearly useless: Docker routes to the
   container's bridge IP, not loopback, so a loopback-bound container is unreachable even when
   published, which is why the image sets `0.0.0.0`.)
2. The publish (`docker run -p …`) is the real exposure decision:
   - No `-p`: reachable only from other containers on the same user-defined network
     (`http://nalanda:8842/trigger`). This is all you need when Radarr/Sonarr also run in
     Docker; the token then only ever crosses the internal bridge, so plain HTTP is fine.
   - `-p 8842:8842`: binds `0.0.0.0:8842` on the host, reachable from the LAN. This is where
     "trusted network, TLS reverse proxy in front" applies, since the token is sent in clear.
     Needed when Radarr/Sonarr are not containers on the same network.
   - `-p 127.0.0.1:8842:8842`: publishes to host loopback only (for example, for a reverse
     proxy on the same host).

> Docker publishes ports through its own iptables chain, which sits ahead of `ufw`, so a
> `-p`-published port can be reachable from the LAN even when `ufw` appears to block it.
> Bind-publish to a specific interface (`-p 127.0.0.1:8842:8842`) rather than relying on
> `ufw`. Likewise, `--network host` drops the namespace isolation entirely, so the container's
> `0.0.0.0` bind becomes the host's.

## AI use and contributions

Every change to Nalanda is reviewed by a human, but the code itself is written with
AI. If you prefer fully hand-written software, this may not be the project for you.

AI-generated issues and pull requests are welcome, but you must review them thoroughly
yourself before submitting, to the same bar the code is held to. Those that appear not to
have been will be closed.

## Attribution

Nalanda is built on metadata from these providers:

- Metadata provided by [TheTVDB](https://thetvdb.com/). Please consider adding missing
  information or [subscribing](https://thetvdb.com/subscribe).
- This product uses the TMDB API but is not endorsed or certified by
  [TMDB](https://www.themoviedb.org/).
- List and cross-source rating data via [MDBList](https://mdblist.com/).

## License

Nalanda is licensed under the GNU Affero General Public License v3.0 or later
(AGPL-3.0-or-later). See [`LICENSE`](LICENSE) for the full text.
