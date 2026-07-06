# Infrastructure

This document describes the runtime infrastructure for the Stock Move Explainer service: what runs where, how data flows between components, and how to operate it locally. See [README.md](README.md) for feature/setup docs.

## Components

| Component | What | Where it runs |
|---|---|---|
| API server | FastAPI app (`app/main.py`), served by Uvicorn (`--reload`) | Docker container (`api` service), via `docker-compose.yml` |
| Ingestion worker | `app/scheduler.py` loop, calls `app/ingestion.py` | Docker container (`ingestion` service), via `docker-compose.yml` |
| Database | PostgreSQL 16 | Docker container (`db` service), via `docker-compose.yml` |
| Stock prices | `yfinance` (unofficial Yahoo Finance client) | External, called over HTTPS, no key required |
| News | `yfinance`'s `Ticker.news` (Yahoo Finance) | External, called over HTTPS, no key required |
| Chat LLM | Anthropic Claude API | External, requires `ANTHROPIC_API_KEY`. Called from the `api` container (chat, live) **and** from ingestion (explanation generation, batched, cached) |

**Everything runs in Docker** - there's no "start the server locally" step. `docker compose up` (via `./scripts/infra.sh start`) brings up all three containers.

**Ingestion and consumption are separate processes.** The API server never calls `yfinance` or the news provider itself — it only reads/writes Postgres. All external data fetching happens in the `ingestion` worker (scheduled) or via the `POST /api/ingest/{ticker}` endpoint (manual, but still routed through the same `app/ingestion.py` code, run in the `api` container for that one request). The API and the worker communicate only through the `db` container — there's no direct API↔worker link (no shared queue, no RPC). The one exception to "the API never calls an external service to answer a request" is `POST /api/chat`, which does call Claude live - everything else the API serves is DB-only.

## Docker Compose

Defined in [docker-compose.yml](docker-compose.yml). Three services:

- **`db`** — `postgres:16-alpine`
  - Credentials: user/password/db all `stockmoves` (local dev only, not meant for any shared/prod environment)
  - Port: `5432` published to the host
  - Volume: `pgdata` (named volume) — survives `docker compose down`, removed only by `docker compose down -v`
  - Healthcheck: `pg_isready`, every 5s, 10 retries

- **`api`** — built from the repo's [Dockerfile](Dockerfile), `command:` overridden to `uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload`
  - Port `8000` published to the host
  - `./app` is bind-mounted to `/app/app` (the image's own copy from `COPY app ./app` at build time), so editing code on the host is what the running container sees - Uvicorn's `--reload` (via `watchfiles`, part of the `uvicorn[standard]` extra) picks it up and restarts the app process within a second or two, with no image rebuild
  - Waits for `db`'s healthcheck before starting
  - `DATABASE_URL` overridden to the in-network hostname `db`; `ANTHROPIC_API_KEY`/`ANTHROPIC_MODEL` come from the shell via `${VAR}` (resolved from 1Password by `op run`, see below), everything else from `env_file: .env`
  - A dependency added to `pyproject.toml` is **not** picked up by the mount (only `app/` is mounted, not the installed packages) - that needs `docker compose up -d --build api`

- **`ingestion`** — same [Dockerfile](Dockerfile), default `CMD` (`python -m app.scheduler`), no bind mount (rebuild needed to pick up code changes here)
  - Waits for `db`'s healthcheck before starting
  - `DATABASE_URL` overridden the same way as `api`; `INGESTION_INTERVAL_SECONDS` (default `21600` = 6h, from `.env`) controls how often it refreshes tracked tickers
  - `restart: unless-stopped` — the loop itself already catches per-ticker exceptions (see `app/ingestion.py`), so a container restart would only matter if the process crashed outright

Both `api` and `ingestion` read `env_file: .env`, so **`.env` must exist before `docker compose up`** (Compose fails fast otherwise) — it's checked into the repo, so this is normally already satisfied. `.env` holds 1Password references (`op://...`), not literal secrets: the four secret vars (`ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `EXA_API_KEY`, `GNEWS_API_KEY`) are also set explicitly under **both** services' `environment:` blocks as `${VAR}`, which takes precedence over `env_file` for those keys and is resolved from the shell — `scripts/infra.sh` runs `docker compose up` wrapped in `op run --env-file .env --`, which signs into 1Password (if needed) and injects the real values into that shell before Compose reads them. (Both services need all four: `POST /api/ingest/{ticker}` in the `api` container and the scheduler loop in `ingestion` both run the same classification/news-provider code path in `app/ingestion.py`. Relying on just `env_file` for either would silently hand that container the *unresolved* `op://...` string instead of the real key or an empty value.)

Start/stop via [scripts/infra.sh](scripts/infra.sh) (preferred over installing/running Postgres directly on the host, or starting Uvicorn by hand):

```bash
./scripts/infra.sh start     # docker compose up -d, then shows container status (checks .env exists first)
./scripts/infra.sh stop      # docker compose down, keeps the pgdata volume
./scripts/infra.sh restart   # stop then start
./scripts/infra.sh reset     # stop AND delete the pgdata volume (asks for confirmation)
./scripts/infra.sh status    # docker compose ps
./scripts/infra.sh logs [db|api|ingestion]   # docker compose logs -f, all services or one
```

It's a thin wrapper around plain `docker compose` commands - nothing it does can't be done by hand, it just standardizes the workflow (and checks for `.env` up front with a clearer error than Compose's own).

## Database schema

Tables are created automatically via SQLAlchemy's `Base.metadata.create_all` (`app/db.py:init_db`) — there is no migration tool (Alembic) in place; schema changes currently mean editing `app/db.py` and rebuilding/re-running against a fresh or manually-altered database. `create_all` is idempotent, so both the `api` container (`app/main.py`'s startup event) and the `ingestion` container (`app/scheduler.py:main`) call it - either one can be the first to touch a brand-new database and the schema still ends up correct. (Earlier this was only called from the API; running the real stack via Docker for the first time surfaced the ordering bug where the worker crashed against a schema-less database if it started first, which is what prompted adding it to the worker too.)

### `prices`
One row per ticker per trading day. Primary key `(ticker, date)`.

| Column | Type | Notes |
|---|---|---|
| `ticker` | string | e.g. `AAPL` |
| `date` | date | trading day |
| `open`, `high`, `low`, `close` | float | adjusted (`auto_adjust=True` in yfinance) |
| `volume` | integer | |
| `pct_change` | float, nullable | `% change` vs. the previous trading day's close |

### `ticker_price_coverage`
One row per ticker. Tracks the contiguous date range already ingested, and doubles as the **tracked-tickers list**: a ticker exists here once it's been ingested at least once (via the manual endpoint), and the scheduler uses this table to know what to refresh — it never discovers new tickers on its own. Exposed directly via `GET /api/tickers`.

| Column | Type | Notes |
|---|---|---|
| `ticker` | string, PK | |
| `min_date` / `max_date` | date | inclusive range already persisted for this ticker |

### `articles`
News articles, deduped per ticker+URL. Unique constraint `(ticker, url)`.

| Column | Type | Notes |
|---|---|---|
| `id` | integer, PK, autoincrement | |
| `ticker` | string, indexed | |
| `title`, `url`, `source`, `summary` | string, nullable except title/url | |
| `published_at` | timestamptz, nullable | used to align articles to price movements |
| `fetched_at` | timestamptz | when this row was written, for debugging/observability |

### `movement_explanations`
A cached, pre-generated explanation for one ticker's movement on one day. Generated at ingestion time (`app/explain.py`), never on read - this is what lets `GET /api/tickers/{ticker}` return a "why" with no added latency and no dependency on Claude being reachable. Primary key `(ticker, date)`.

| Column | Type | Notes |
|---|---|---|
| `ticker` | string, PK | |
| `date` | date, PK | the movement's date, same `date` as in `prices` |
| `explanation` | string | 1-2 sentence, Claude-generated |
| `model` | string | which Anthropic model produced it |
| `generated_at` | timestamptz | when this row was written |

## Data flow

### Ingestion (writes) — `app/ingestion.py`, the only module that calls `yfinance`/the news provider

Triggered two ways:
1. **Scheduled**: the `ingestion` container's loop (`app/scheduler.py`) calls `ingest_tracked_tickers()` every `INGESTION_INTERVAL_SECONDS`, which reads every ticker out of `ticker_price_coverage` and refreshes each one.
2. **Manual**: `POST /api/ingest/{ticker}` calls the same underlying `ingest_ticker()` synchronously, in the API process, for that one request. This is both the "do it now" override and how a ticker starts being tracked at all.

Per ticker, `ingest_ticker()`:
1. Looks up existing coverage. If none, backfills a default 180-day lookback; if some exists, extends forward to today (or to an explicit `start_date`/`end_date` if the caller passed one).
2. Calls `yfinance` for the union of the requested range and existing coverage (plus a small lookback buffer so the first day's `%` change has a real previous close).
3. Upserts price rows (`ON CONFLICT ... DO UPDATE`) and extends `ticker_price_coverage`.
4. Best-effort fetches news and upserts articles (`ON CONFLICT ... DO NOTHING`, deduped by URL) — failures here are logged and swallowed so a news hiccup never blocks price ingestion.
5. Best-effort generates and stores explanations (`app/explain.py`) for movements in the just-fetched range that meet a fixed 2% threshold (`EXPLANATION_MIN_MOVE_PCT`), don't already have a stored explanation, and have at least one nearby article to work from. One batched Claude call covers every qualifying movement for that ingestion run (not one call per movement) - also wrapped in its own try/except, so a Claude failure or missing `ANTHROPIC_API_KEY` never blocks price/news ingestion either.

Because Yahoo's news feed is only ever the ~10 most recent stories, persisting each fetch means the `articles` table accumulates a growing historical archive the more often ingestion runs — something a stateless, fetch-on-request version of this app couldn't do. The same logic means most historical movements (outside that ~10-story recent window) never get an explanation generated, since step 5 requires at least one nearby article - this is the same underlying limitation as the news coverage one, just visible in a different field.

### Consumption (reads) — `app/analysis.py`, used by both REST endpoints, never calls external APIs

1. Looks up `ticker_price_coverage`. If the ticker has never been ingested → raise `TickerNotIngestedError` (`GET`/`POST /api/chat` both surface this as `404`, pointing the caller at `POST /api/ingest/{ticker}`).
2. Reads `prices`/`articles` for the requested range straight from Postgres — no coverage math, no fallback fetch.
3. Computes movements and attaches nearby articles.
4. Reads any pre-generated `movement_explanations` rows for those same dates and attaches them - purely a lookup, no Claude call happens here even if a movement has no stored explanation yet.
5. Returns both the requested range and the ticker's actual `data_coverage_start`/`data_coverage_end`, so a caller can tell if the worker hasn't caught up to "today" yet (the requested range can legitimately extend past what's been ingested if the scheduler hasn't run recently).

## Configuration

All runtime config is environment variables, loaded via `app/config.py` (`pydantic-settings`, reads `.env`). Secret values in `.env` are 1Password references (`op://...`), resolved at runtime via `op run` (see `scripts/infra.sh`) rather than stored on disk. See [.env](.env) for the full list:

- `DATABASE_URL` — the value in `.env` (`localhost:5432`) is for anything running on the host, i.e. only `pytest`/`psql` today. Both the `api` and `ingestion` Compose services override this to the in-network hostname `db` instead — see `docker-compose.yml`.
- `INGESTION_INTERVAL_SECONDS` — only read by `app/scheduler.py`; irrelevant to the API process.
- `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` — required for `POST /api/chat`, and read by the `ingestion` container too (explanation generation and industry/competitor classification at ingest time both skip gracefully, logging and doing nothing, if this isn't set - neither fails the ingestion run).
- `EXA_API_KEY` — optional; activates industry moves (industry/competitor news via `ExaProvider`, see `app/news/exa_provider.py`) on top of the always-on yfinance company news. Unset, behavior is unchanged.
- `GNEWS_API_KEY` — unused today (no provider reads it); reserved for a possible second broad-news provider.

## Operating notes / known gaps

- **No migrations.** Schema changes require dropping/recreating tables or hand-writing `ALTER`s; fine for a single-developer demo, not for a shared environment.
- **No connection pooling config beyond SQLAlchemy defaults.** Not a concern at demo scale (single process, low concurrency).
- **Local-only Postgres credentials.** `stockmoves`/`stockmoves` in `docker-compose.yml` and `.env` are placeholders — rotate before ever exposing this beyond localhost.
- **Scheduler is a plain sleep loop, not a real job runner.** `app/scheduler.py` is `while True: ingest_tracked_tickers(); sleep(interval)` — one process, one global interval, no per-ticker schedules, no retry/backoff beyond "try again next loop." Fine for one worker; would need APScheduler/Celery/cron if this grew to need per-ticker cadences or multiple coordinating workers.
- **Hot reload only covers `app/`.** The bind mount on the `api` service is `./app:/app/app`; `pyproject.toml` changes require `docker compose up -d --build api`. The `ingestion` container has no mount at all (it's a long-running loop, not something you iterate on live the same way), so code changes there also need a rebuild.
- **Cold start ordering.** The `ingestion` container can start before any ticker has ever been ingested — it'll just log "no tracked tickers yet" every interval until the first `POST /api/ingest/{ticker}` call happens. This is expected, not an error.
- **Ingestion now optionally depends on Claude.** Before explanation generation existed, ingestion only ever talked to `yfinance`, and could run indefinitely with zero LLM dependency. Now a missing/unreachable Claude doesn't fail ingestion (price/news still persist, explanation generation is skipped and logged) but it does mean movements won't get a stored explanation until Claude is reachable on some later ingestion run for that ticker.
- **`ANTHROPIC_MODEL` falls back to a default if resolved as an empty string** (`app/config.py`, a `field_validator` on `Settings.anthropic_model`). This was found via real testing, not by inspection: an `op://` reference to a vault field that doesn't exist resolves to `""`, and pydantic-settings treats an explicitly-set empty env var as overriding the field's default - the Anthropic API then rejects `model=""` outright. The same failure mode existed for `POST /api/chat` before explanation generation was added; it just hadn't been exercised yet.
- **Verification history:** the persistence layer and the ingestion/consumption split were first verified against a Homebrew-installed local Postgres (Docker wasn't available in that sandbox yet), then re-verified end-to-end against the real `docker-compose.yml` stack once Docker became available - including rebuilding the `ingestion` image and confirming it picks up a ticker ingested through the API purely via the shared `db` container. Once the `api` service was containerized too, hot reload was verified directly: edited `app/main.py`'s `/health` handler on the host while the stack was running, confirmed via `docker compose logs api` that Uvicorn's `WatchFiles` reloader picked it up and restarted within ~2 seconds with no manual intervention, then reverted the edit and confirmed the same again. Explanation generation was verified against the real running stack too: a synthetic movement + article round-tripped through a real Claude call and back into Postgres, then confirmed visible on `GET /api/tickers/{ticker}`, before test data was cleaned up.
