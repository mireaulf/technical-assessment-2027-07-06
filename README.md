# Stock Move Explainer

Explains major single-day stock price moves using related news, via a small FastAPI service.

Infrastructure details (Docker Compose, database schema, data flow, config) live in [INFRASTRUCTURE.md](INFRASTRUCTURE.md).

## What it does

1. Ingests historical daily prices (`yfinance`) and recent news for a ticker, persisting them to Postgres.
2. Flags days where the close moved >= a threshold vs. the previous close (default 2%).
3. Attaches news to movements that fall near their date, and pre-generates a plain-English explanation (Claude) for movements that have nearby articles - at ingestion time, not on read.
4. Ingestion (talking to `yfinance`/Claude) and consumption (serving requests) are separate concerns:
   - `POST /api/ingest/{ticker}` — fetch a ticker's data now, persist it, and generate explanations for any newly-qualifying movements. This is also how a ticker starts being tracked.
   - A separate `ingestion` worker (`docker compose`, `app/scheduler.py`) refreshes every already-tracked ticker on an interval, so ingestion doesn't depend on anyone hitting the API.
   - `GET /api/tickers` — list every ticker that's been ingested, with its data range.
   - `GET /api/tickers/{ticker}` — full stock + news data for a ticker, filterable by date range and move threshold, including any pre-generated explanation per movement. **Reads only from Postgres**, never calls `yfinance` or Claude.
   - `POST /api/chat` — ask questions about a ticker's movements; grounds Claude's answer in the same DB-only data (including any pre-generated explanations). Also never calls `yfinance` directly.

## Setup & run

Everything - Postgres, the API, and the ingestion worker - runs in Docker. There's nothing to start manually.

`.env` is already checked in - it holds 1Password references (`op://...`), not literal secrets, so there's nothing to copy or edit. `./scripts/infra.sh start` signs you into 1Password if needed and resolves them at runtime via `op run`. It assumes a vault named `technical-assessment-2026-07-06` with a document called `local` in it.

```bash
./scripts/infra.sh start   # builds and starts Postgres, the API, and the ingestion worker
```

The API is then up at `http://localhost:8000` (interactive docs at `/docs`). `app/` is bind-mounted into the `api` container and Uvicorn runs with `--reload`, so editing code on the host reloads it inside the container immediately - no rebuild, no restart. A code change that adds a new dependency to `pyproject.toml` does need a rebuild: `docker compose up -d --build api`.

Tables are created automatically on startup (`app/db.py:init_db`) by both the API and the ingestion worker.

A ticker has to be ingested before you can query or chat about it — either wait for the background worker to pick it up, or trigger it directly:

```bash
curl -X POST "http://localhost:8000/api/ingest/AAPL"   # fetch + persist AAPL now; starts tracking it

curl "http://localhost:8000/api/tickers"   # list every tracked ticker + its data range

curl "http://localhost:8000/api/tickers/AAPL?min_move_pct=2&start_date=2026-01-01&end_date=2026-07-01"

curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"ticker": "AAPL", "message": "What drove the biggest move in the last month?"}'
```

Managing the stack: `./scripts/infra.sh {start|stop|restart|reset|status|logs}` — see [INFRASTRUCTURE.md](INFRASTRUCTURE.md) for details.

## Tests

Tests run on the host (not in Docker), against Postgres via the port `docker-compose.yml` publishes to `localhost:5432`:

```bash
uv sync

uv run pytest tests/ -v
```

`tests/test_stock_service.py` covers the pure move-detection and news-alignment logic (no network, no DB). `tests/test_repository.py` exercises the Postgres cache-aside layer (upsert/dedupe/coverage-union) against a real database — those tests auto-skip if Postgres isn't reachable (i.e. the stack isn't running), so `pytest` still passes cleanly either way.

## Architecture & key decisions

- **Movement definition**: single-day `% change` vs. previous close, threshold configurable per-request (default 2%, per the assignment's example). Simple, transparent, and matches how "a stock moved X% today" is commonly reported.
- **News as a pluggable interface** (`app/news/base.py`): I don't have a NewsAPI/GNews/Exa key, so the only implementation wired up is `YFinanceNewsProvider`, which uses `yfinance`'s built-in news feed (no key required) and covers the **[Easy]** company-specific tier. Everything else — the analysis pipeline, the endpoints, the chat context builder — depends only on the `NewsProvider` interface, so a `NewsAPIProvider` or `ExaProvider` for the **[Medium]/[Hard]** tiers (competitor/industry, macro/political news) can be dropped in without touching the rest of the app.
- **Known limitation of the default provider**: Yahoo's news feed only exposes a rolling window of the ~10 most recent stories per ticker — it's not a queryable historical archive. Articles are attached to a movement only if their publish date falls within a couple of days of it, so **recent movements get real headlines, older ones will legitimately show up with no articles**. This is a real trade-off of running with zero paid API keys, not a bug; a historical news API would remove it.
- **Chat endpoint**: stateless per request — the client resends conversation history if it wants multi-turn context. The server pulls the same movement/news dataset the `/api/tickers` endpoint would return, formats it as plain text, and passes it to Claude as a system prompt so answers are grounded in the actual data rather than the model's general knowledge. It explicitly instructs the model to say when the data doesn't cover something rather than guess.
- **Persistence (Postgres via `docker compose`)**: daily price bars are immutable once the market closes, so re-fetching them from `yfinance` on every request is pure waste. `app/db.py` / `app/repository.py` implement a cache-aside layer:
  - A `ticker_price_coverage` table tracks the `[min_date, max_date]` already fetched per ticker. A request whose range falls entirely inside existing coverage (and doesn't touch today, since today's bar can still move intraday) is served **entirely from Postgres — zero `yfinance` calls**. Verified locally: a sub-range re-request dropped from ~750ms to ~30ms once cached.
  - A request outside current coverage triggers one `yfinance` fetch spanning the union of the old and new ranges (plus a small lookback buffer so the first day's `%` change has a real previous close), upserts it, and extends the coverage row — so the cached range only ever grows.
  - Movement detection was refactored to operate on `PricePoint`s rather than a raw dataframe, so the exact same logic runs whether the data just came from `yfinance` or was read back from Postgres.
  - Articles are upserted too (deduped on `(ticker, url)`). Since Yahoo's feed is only ever the ~10 most recent stories, persisting each fetch means the news archive for a ticker actually *grows* the more the app is used, partially offsetting the "no historical news API" limitation over time.
  - Trade-off: this is Postgres in Docker rather than SQLite specifically because it's the more realistic choice if this were to grow into a real service (proper concurrent writes, richer types) — at the cost of requiring `docker compose up` before the app will run, versus SQLite's zero-setup single file.
- **Ingestion decoupled from the API** (`app/ingestion.py`, `app/scheduler.py`): the API used to fall back to fetching from `yfinance` inline whenever a request landed outside cached coverage. That means the request path's latency and reliability depended on Yahoo's API being up, and it made "the API" and "the thing that talks to yfinance" the same failure domain. Now:
  - `app/ingestion.py` is the *only* module that imports `yfinance`/the news provider. `app/analysis.py` (the read path used by both REST endpoints) only ever queries Postgres and raises `TickerNotIngestedError` (404) if a ticker has no data yet — it never fetches on the caller's behalf.
  - A ticker starts being tracked the first time it's ingested — there's no separate "watchlist" config, `POST /api/ingest/{ticker}` both onboards new tickers and refreshes existing ones. The `ingestion` container then keeps every already-tracked ticker refreshed on a timer (`INGESTION_INTERVAL_SECONDS`, default 6h) via `app/scheduler.py`'s loop.
  - This is a real, if simple, separate process: it's its own Docker Compose service and Dockerfile, not a background thread inside the API. The API and the worker only communicate through Postgres.
  - Trade-off: the scheduler is a plain `while True: ...; sleep(interval)` loop rather than something like Celery/APScheduler/cron — sufficient for one worker on one schedule, but it wouldn't scale to per-ticker schedules, retries with backoff, or multiple workers coordinating without adding a real job queue.
  - The response now separates the requested range (`start_date`/`end_date`) from what's actually persisted (`data_coverage_start`/`data_coverage_end`), so a caller can tell if the worker hasn't caught up yet.
- **Movement explanations are generated at ingestion time, not on read** (`app/explain.py`, wired into `ingest_ticker` in `app/ingestion.py`). Originally the "why" was only ever synthesized live, inside the chat endpoint. The trade-off going the other way: ingestion already runs on a schedule (and ad hoc via the manual endpoint), so paying the LLM cost up front means `GET /api/tickers/{ticker}` can return a ready-made explanation with zero added latency and zero dependency on Claude being up when a customer actually asks - reliability and speed on the read path matter more here than avoiding upfront spend, especially since one company's move is often relevant context for others in the same trade later.
  - One batched Claude call per ingestion run covers every movement in the just-fetched range that (a) meets a fixed 2% threshold and (b) already has nearby articles attached, and skips entirely if there's nothing to explain or no `ANTHROPIC_API_KEY` configured - a missing/failing LLM call never blocks price/news ingestion, it's wrapped in its own try/except.
  - Stored in a `movement_explanations` table keyed by `(ticker, date)`; only ever written once per date unless the row doesn't exist yet, so re-ingesting a ticker doesn't re-spend on movements already explained.
  - On read (`app/analysis.py`), a movement with no nearby articles gets an explicit `"No news coverage available for this date."` in its `explanation` field rather than a bare `null` - distinguishing "nothing to explain from" (expected, most historical movements) from a movement that had articles but genuinely has no stored explanation yet (rarer - a prior Claude call failed, or `ANTHROPIC_API_KEY` wasn't set when it was last ingested), which stays `null`.
  - Trade-off: the threshold used to decide what's "explanation-worthy" at ingest time is fixed (2%), separate from the per-request `min_move_pct` the read endpoints accept - a request with a looser threshold can surface extra movements that were never candidates for a stored explanation. Also, a first-time backfill for a brand-new ticker can trigger many explanations in one ingestion run, since it's evaluating the ticker's whole default lookback window at once, not just one day.
- **The API itself runs in Docker too** (`api` service in `docker-compose.yml`, same `Dockerfile` as the ingestion worker with `command:` overridden to run Uvicorn instead). `app/` is bind-mounted over the image's copy and Uvicorn runs with `--reload`, so there's no "start the server" step separate from `docker compose up` and no loss of the fast edit/test loop you'd get running it locally. Trade-off: a change to `pyproject.toml` isn't picked up by the mount, since dependencies are installed at build time — that path needs an explicit `--build`.
