# Changelog

All notable changes to Nalanda are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-06-23

### Fixed

- Per-item metadata writes (`run metadata`) returned HTTP 500 from Jellyfin for any item
  with trickplay data. The read-modify-write echoed the full item DTO back to
  `POST /Items/{id}`, including the `Trickplay` field, which Jellyfin serializes but cannot
  deserialize (`TrickplayInfoDto`'s constructor parameters don't round-trip). The field is
  now stripped before the update; items without trickplay data were unaffected.

## [0.1.0] - 2026-06-22

_Initial release. There are no prior versions, so the list below is the feature set delivered in
the first version rather than changes between releases._

### Added

#### Configuration
- Collections and builders defined in `config.yml`, with secrets and connection details in
  `.env`, kept separate and never committed.
- A JSON Schema generated from the code (`nalanda schema`) for live editor validation; unknown
  keys are rejected so typos surface immediately.
- Global `settings` (sync mode, language, region, `hide_year`, Radarr/Sonarr defaults, webhook,
  cache, scheduling) with per-collection overrides. `language` (TMDB text metadata) and `region`
  (release dates on charts/discover) are separate axes.
- A `media: movie | tv | mixed` discriminator per collection (required, no default). The domain
  model is a unified `MediaItem` (`tmdb_id` / `tvdb_id` / `imdb_id`); movie-only and TV-only
  builder keys are rejected on the wrong media, and shared keys dispatch to the right endpoint.

#### Builders: TMDB
- Movie sources: `tmdb_collection`, `tmdb_movie`, `tmdb_title` (by name, `(YYYY)` to
  disambiguate), `tmdb_list`, `tmdb_keyword`, `tmdb_genre`, people (`tmdb_actor` /
  `tmdb_director` / `tmdb_writer` / `tmdb_producer` / `tmdb_crew`), `tmdb_company`, the charts
  (`tmdb_popular`, `tmdb_now_playing`, `tmdb_top_rated`, `tmdb_upcoming`, `tmdb_trending_daily`,
  `tmdb_trending_weekly`), and a raw `tmdb_discover` escape hatch.
- TV sources: `tmdb_show`, `tmdb_network`, the TV charts `tmdb_on_the_air` / `tmdb_airing_today`,
  and TV dispatch for the shared keys (title / genre / keyword / people / discover / list, and
  the popular / top_rated / trending charts).
- Operators: per-key `all` / `any` / `except`; cross-key intersection (`match: all`); ordered
  composition (`append:`); and `limit:` (defaults to 100, `0` for unlimited). Names or ids.

#### Builders: TVDB
- `tvdb_show`, `tvdb_movie` (TVDB tracks movies too, resolved to TMDB/IMDb for Radarr/Jellyfin),
  `tvdb_list`, and a raw `tvdb_discover` filter. Ships a bundled per-project API key (obfuscated;
  rotate via `scripts/encode_tvdb_key.py`), so no per-user setup is needed.

#### Builders: MDBList
- `mdblist_list`: list(s) by URL/ref with `url` / `all` / `except` operators, `sort_by` and
  `limit`; cursor pagination; external and user lists.
- `mdblist_catalog`: discover by cross-source ratings TMDB can't expose (Rotten Tomatoes,
  Metacritic, IMDb, Letterboxd, MDBList score) plus genre/country/language/year/runtime.
- `mdblist_official`: official playlists by slug.
- Movies and shows: a `media: tv` collection reads the show list / `/catalog/show`.

#### Jellyfin collections
- Create / sync / append membership reconciliation (of movies or whole shows), comparing against
  the server and writing only what changed.
- Full metadata ownership: overview and all three image slots (Primary / Thumb / Backdrop),
  auto-sourced from TMDB, overridden per slot, or supplied from a local artwork repository (see
  Artwork). The image last applied (URL, or content hash for a local file) is tracked in a state
  file so it re-applies only on change.
- Display order (`source` / `sort_name` / `release_date`) with a smart default.
- `sections` to group collections in the library view via generated sort titles, plus a
  `sort_title` override.
- `hide_year`, `libraries` scoping, and `delete_unconfigured_collections` (managed collections
  only).

#### Mixed collections
- `media: mixed`: one Jellyfin BoxSet holding both movies and shows (for example a whole
  franchise of films and series). Media-specific keys produce their own media; shared keys
  (genre, people, charts, `mdblist_*`, …) produce both. A mixed collection can carry both a
  `radarr:` block (its movies) and a `sonarr:` block (its shows), each with its own identity tag,
  so either a Radarr or a Sonarr webhook scopes a run to it. Provider-id matching, dedupe, and
  library indexing are scoped by media type, so a movie and a show that share a numeric TMDB id
  can't collide.

#### Artwork
- Per-slot image overrides for each collection: `primary_art_url` / `thumb_art_url` /
  `backdrop_art_url` (a remote URL Jellyfin downloads) or `primary_art_file` / `thumb_art_file` /
  `backdrop_art_file` (a local file whose bytes are uploaded). URL and file are mutually
  exclusive per slot; either overrides the auto-sourced TMDB image.
- A local artwork repository via a top-level `artwork_repo:` (`path`): for every collection
  Nalanda auto-discovers `<path>/collections/<slug>/<primary|thumb|backdrop>.<png|jpg|jpeg|webp>`
  (extension precedence `png` > `jpg`/`jpeg` > `webp`), where `<slug>` is the collection name
  slugified, the same slug as its Radarr/Sonarr identity tag. `create_empty_folders` scaffolds a
  drop-folder per collection; `delete_old_empty_folders` prunes empty folders for collections no
  longer configured (a folder containing files is never removed).
- Per-slot resolution precedence: explicit `*_art_url` / `*_art_file` → artwork-repo file →
  auto-sourced TMDB → none. An invalid explicit value (bad URL / missing file) warns and falls
  through; local files are content-hashed, so editing a file re-uploads it while an unchanged run
  stays a no-op.

#### Per-item metadata
- A top-level `metadata:` block (grouped into `movies` and `shows`) that writes and locks
  specific fields on individual titles in Jellyfin, independent of any collection. Each entry is
  keyed by exactly one provider id (`tmdb` / `imdb` / `tvdb`) and sets one or more of
  `parental_rating`, `title`, `overview`, `sort_title`. The lockable fields are locked so a
  metadata refresh can't overwrite them; `sort_title` persists via `ForcedSortName`, re-asserted
  each run. Compare-then-write, tracked in its own state file (`.nalanda-metadata-state.json`).
- `run metadata` (dry-run aware) writes the overrides on demand; the daemon runs the job on
  `settings.jobs.metadata`. `settings.unlock_unconfigured_metadata` (default false) unlocks a
  field when its entry is removed from config, so a Jellyfin refresh can reclaim it.

#### Radarr integration (optional)
- Per-collection `radarr:` block (opt-in via `enable: true`, default `false`) inheriting global
  `settings.radarr` defaults.
- Add missing movies, reconcile quality profile (`upgrade_existing`) and monitoring
  (`monitor_existing`), search on add (decoupled from tagging), and `minimum_availability`.
- A Nalanda-owned identity tag per collection (`nalanda-<slug>`) that tracks desired
  membership, namespace-isolated so other tags are never touched; a configurable stale
  policy (`mark` / `delete` / `keep`) with symmetric rejoin; reconciled in bulk.

#### Sonarr integration (optional)
- Per-collection `sonarr:` block (the TV analogue of `radarr:`, opt-in via `enable: true`)
  inheriting `settings.sonarr`: add missing shows, reconcile quality profile / monitored flag, an
  identity tag with the same three-state stale policy and symmetric rejoin, search / cutoff-search
  on add, `series_type`, `season_folder`, and the Sonarr-v3 `language_profile`. Shows match
  tvdb-first, then tmdb/imdb.
- The `monitor` strategy (all / future / missing / existing / pilot / first_season /
  latest_season / none): which episodes/seasons a show monitors. Applied at add; with
  `monitor_existing` the deterministic strategies reconcile per-season monitoring each run.

#### Webhook daemon (`nalanda serve`, optional)
- A stdlib HTTP daemon so Radarr/Sonarr (or anything) can push a re-sync.
- `POST /trigger` (reactive, always scoped to the collections in the payload's `nalanda-` tags),
  `POST /run` (explicit full run, gated by `allow_full_run`), and `GET /health`.
- Optional shared-secret auth via the `X-Nalanda-Token` header (constant-time), with a request
  body-size cap. `WEBHOOK_SECRET` is optional: leave it blank to run on schedules only, where
  `/health` and cron still work and the POST routes return 503; set it to enable them.
- A debounce window that coalesces a burst of triggers into one run, and a single run-lock so
  runs never overlap (coalescing and superseding are keyed by job kind).
- Cron scheduling via a three-level cascade (a collection's `run_schedule:` > the per-job-kind
  default `settings.jobs.<kind>` > the global `settings.run_schedule`), with reusable named crons
  in `settings.run_schedules`, inline crons, and a `none`/`disabled` opt-out; every value is
  validated at load. Per-collection schedules run scoped; the global/job-default cron also prunes.
- Deployment settings live in the environment / `.env` (`NALANDA_CONFIG`, default `./config.yml`;
  `NALANDA_HOST`, default `127.0.0.1` loopback; `NALANDA_PORT`, `8842`), with a
  real env var overriding the `.env` value (the Docker image sets `NALANDA_HOST=0.0.0.0`). `.env`
  is read from the same directory as the config file. `settings.webhook` holds only
  inbound-request behaviour: `debounce_seconds`, `max_wait_seconds`, `allow_full_run`.

#### Metadata cache
- A disposable on-disk cache (SQLite, `.nalanda-cache.db` beside the config) of what TMDB, TVDB,
  and MDBList return, so the daemon's per-webhook and per-cron runs don't re-fetch near-identical
  data. A missing, stale, or corrupt cache only ever costs speed, never correctness; Jellyfin and
  Radarr/Sonarr library state is never cached.
- `settings.cache` with an `enabled` switch and four intent-named durations
  (`record` / `list` / `query` / `chart`; `<int>d` / `<int>h`, or `0`/`off` to bypass a bucket,
  minutes rejected). A `cache info | prune | clear [namespace]` command inspects and maintains it.

#### Attribution
- TheTVDB / TMDB / MDBList credited in the README; the TVDB attribution is also logged once per
  run whenever a `tvdb_*` builder is used.

#### Tooling & operations
- `--dry-run` for read-only previews that log every intended Jellyfin/Radarr/Sonarr write, and
  `run --refresh-cache` to re-fetch and re-store the cache for one run.
- CLI commands: `run` (global; `run collections [names]` or `run metadata` for a single job),
  `build`, `serve`, `schema`, `cache`, and a bare read-only source report (a TMDB collection id,
  an MDBList list, or `tvdb:<ref>` for a show).
- Client-side rate limiting (token bucket) on the metadata sources -- TMDB at 40 req/s, TVDB at
  10 req/s, MDBList at 1 req/s (5 req/s when a supporter key is detected) -- plus automatic retry
  with `Retry-After`-aware backoff on 429 and transient 5xx responses across all clients.
- A cross-process run lock so a manual `run` and the daemon coordinate (a contended run waits,
  then proceeds with full output).
- A rootless Docker image for the daemon (runs as uid/gid `1000`, `tini` as PID 1, a bundled
  healthcheck) with a single `/config` mount holding `config.yml`, the co-located `.env`, and the
  state file, plus `docker/compose.example.yml`. A first `run`/`serve` with no config seeds a
  starter `config.yml` + `.env`, so a fresh deployment comes up idle and healthy instead of
  erroring (an unwritable config directory, e.g. a root-owned bind mount, yields an actionable
  message rather than a traceback). Config-file path configurable via `NALANDA_CONFIG`; the state
  and cache files live beside it.
