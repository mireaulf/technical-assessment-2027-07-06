# Stock Move Explainer

Explains major single-day stock price moves using related news, via a small FastAPI service.

Infrastructure details (Docker Compose, database schema, data flow, config) live in [INFRASTRUCTURE.md](INFRASTRUCTURE.md).

## What it does

1. Ingests historical daily prices (`yfinance`) and recent news for a ticker, persisting them to Postgres.
2. Flags days where the close moved >= a threshold vs. the previous close (default 2%).
3. Attaches news to movements that fall near their date, and pre-generates a plain-English explanation (Claude) for movements that have nearby articles - at ingestion time, not on read.
4. Ingestion (talking to `yfinance`/Claude) and consumption (serving requests) are separate concerns:
   - `POST /api/ingest/{ticker}` — fetch a ticker's data now, persist it, and generate explanations for any newly-qualifying movements. This is also how a ticker starts being tracked. `?force=true` discards existing coverage/prices/articles/explanations/classification first and re-ingests from scratch, instead of extending existing coverage.
   - A separate `ingestion` worker (`docker compose`, `app/scheduler.py`) refreshes every already-tracked ticker on an interval, so ingestion doesn't depend on anyone hitting the API.
   - `GET /api/tickers` — list every ticker that's been ingested, with its data range and (if classified) industry; filterable by `industry` (case-insensitive substring match).
   - `GET /api/industries` — every distinct industry classified so far, to discover valid values for the filter above.
   - `GET /api/tickers/{ticker}` — full stock + news data for a ticker, filterable by date range and move threshold, including any pre-generated explanation per movement. **Reads only from Postgres**, never calls `yfinance` or Claude.
   - `POST /api/chat` — ask questions about a ticker's movements; grounds Claude's answer in the same DB-only data (including any pre-generated explanations). Also never calls `yfinance` directly.

## Setup & run

Everything - Postgres, the API, and the ingestion worker - runs in Docker. There's nothing to start manually.

`.env` is already checked in - it holds 1Password references (`op://...`), not literal secrets, so there's nothing to copy or edit. `./scripts/infra.sh start` signs you into 1Password if needed and resolves them at runtime via `op run`.

```bash
./scripts/infra.sh start   # builds and starts Postgres, the API, and the ingestion worker
```

The API is then up at `http://localhost:8000` (interactive docs at `/docs`). `app/` is bind-mounted into the `api` container and Uvicorn runs with `--reload`, so editing code on the host reloads it inside the container immediately - no rebuild, no restart. A code change that adds a new dependency to `pyproject.toml` does need a rebuild: `docker compose up -d --build api`.

Tables are created automatically on startup (`app/db.py:init_db`) by both the API and the ingestion worker.

A ticker has to be ingested before you can query or chat about it — either wait for the background worker to pick it up, or trigger it directly:

```bash
curl -X POST "http://localhost:8000/api/ingest/AAPL"   # fetch + persist AAPL now; starts tracking it
curl -X POST "http://localhost:8000/api/ingest/AAPL?force=true"   # discard existing data for AAPL and re-ingest from scratch

curl "http://localhost:8000/api/tickers"   # list every tracked ticker + its data range
curl "http://localhost:8000/api/tickers?industry=semiconductor"   # filter by classified industry (substring, case-insensitive)
curl "http://localhost:8000/api/industries"   # every distinct industry classified so far

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
- **News as a pluggable interface** (`app/news/base.py`): the always-on implementation is `YFinanceNewsProvider`, which uses `yfinance`'s built-in news feed (no key required) and covers the **[Easy]** company-specific tier. Everything else — the analysis pipeline, the endpoints, the chat context builder — depends only on the `NewsProvider` interface.
- **Industry moves (competitor/industry news)** is implemented, gated on `EXA_API_KEY` being set (`ANTHROPIC_API_KEY` is already required for chat):
  - The hard part of "competitor/industry" isn't fetching the news, it's knowing what to search for — yfinance doesn't reliably expose a peers/competitors list, and hardcoding a competitor map is brittle and goes stale. `app/news/classifier.py` instead asks Claude to classify a ticker's industry and top publicly-traded competitors from its general knowledge (`{"industry": ..., "competitors": [...]}`), which is then used to drive `ExaProvider` (`app/news/exa_provider.py`) queries — one for the industry, one per competitor — tagging each `Article.category` as `"industry"` or `"competitor"` (vs. `"company"` for the Easy tier).
  - Exa (neural search + clean content extraction) over a keyword-matching news API here, since `industry`/competitor names are loosely-specified terms handed off from an LLM classification rather than exact phrases to search for.
  - That classification is cached per ticker in a `ticker_classifications` table, so it's **one Claude call per ticker, ever** — not one per ingestion cycle. Industry/competitor lists don't change often enough to justify re-deriving them on every refresh; re-classification isn't implemented (a follow-up if a company's peer set genuinely shifts).
  - `app/news/composite_provider.py`'s `CompositeNewsProvider` fans a single `get_news` call out to `YFinanceNewsProvider` + `ExaProvider` and merges the results (deduped by URL); one provider failing doesn't drop the other's articles. `app/ingestion.py` only builds the composite (and only classifies at all) when `EXA_API_KEY` is set — without it, behavior is byte-for-byte the Easy-tier-only behavior from before.
  - `app/chat.py`'s context builder tags non-company articles (e.g. `(industry)`, `(competitor)`) so Claude doesn't conflate broader context with company-specific news when explaining a move.
  - `GET /api/tickers?industry=...` filters the tracked-ticker list by this classification. It's a case-insensitive substring match, not exact equality, as a second line of defense against wording drift (see below). Tickers without a classification yet are excluded when the filter is used.
  - `GET /api/industries` lists every distinct industry classified so far, so a caller can discover valid values for that filter instead of guessing. It's not a fixed/curated list - it's just whatever Claude has derived from tickers ingested with `EXA_API_KEY` set, so it grows over time.
  - There's no fixed industry taxonomy - each ticker is classified independently, which risks two related tickers getting equivalent but differently-worded labels (e.g. "Semiconductors" vs. "Semiconductor Manufacturing"), making the filter/list above less useful for grouping. `_get_or_classify` (`app/ingestion.py`) mitigates this by fetching `list_classified_industries` before classifying a new ticker and passing that list back to Claude, asking it to reuse an existing label when one genuinely fits rather than coining a new one. This is a nudge, not a guarantee - Claude can still pick a new wording - which is why the substring match above exists as a fallback.
  - Not implemented: the **[Hard]** tier (macro/political news) and a `GNewsProvider` - one broad-news provider is enough to prove out the pattern; it would drop in behind the same `NewsProvider` interface.
- **Known limitation of the Easy-tier provider**: Yahoo's news feed only exposes a rolling window of the ~10 most recent stories per ticker — it's not a queryable historical archive. Articles are attached to a movement only if their publish date falls within a couple of days of it, so **recent movements get real headlines, older ones will legitimately show up with no articles** (this doesn't apply to industry moves' industry/competitor articles, which aren't tied to a specific movement date the same way). This is a real trade-off of running with zero paid API keys, not a bug; a historical news API would remove it.
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
  - `POST /api/ingest/{ticker}?force=true` (`reset_ticker`, `app/repository.py`) discards the ticker's existing coverage/prices/articles/explanations/classification and re-ingests as if brand new - the default lookback window (or an explicit `start_date`/`end_date`), not extended/unioned with whatever coverage already existed. Exists as an escape hatch from the caching this whole section describes: normally re-ingesting an already-tracked ticker only extends coverage forward and skips movements that already have a stored explanation, which is the right default but means a bad ingest (e.g. a stale/wrong classification, or explanations generated before a data issue was noticed) has no built-in way to be undone short of a full DB reset. The delete only happens *after* the price re-fetch succeeds, so forcing a bad or renamed ticker returns 404 without touching the ticker's previously-good data.
- **Movement explanations are generated at ingestion time, not on read** (`app/explain.py`, wired into `ingest_ticker` in `app/ingestion.py`). Originally the "why" was only ever synthesized live, inside the chat endpoint. The trade-off going the other way: ingestion already runs on a schedule (and ad hoc via the manual endpoint), so paying the LLM cost up front means `GET /api/tickers/{ticker}` can return a ready-made explanation with zero added latency and zero dependency on Claude being up when a customer actually asks - reliability and speed on the read path matter more here than avoiding upfront spend, especially since one company's move is often relevant context for others in the same trade later.
  - One batched Claude call per ingestion run covers every movement in the just-fetched range that (a) meets a fixed 2% threshold and (b) already has nearby articles attached, and skips entirely if there's nothing to explain or no `ANTHROPIC_API_KEY` configured - a missing/failing LLM call never blocks price/news ingestion, it's wrapped in its own try/except.
  - Stored in a `movement_explanations` table keyed by `(ticker, date)`; only ever written once per date unless the row doesn't exist yet, so re-ingesting a ticker doesn't re-spend on movements already explained.
  - On read (`app/analysis.py`), a movement with no nearby articles gets an explicit `"No news coverage available for this date."` in its `explanation` field rather than a bare `null` - distinguishing "nothing to explain from" (expected, most historical movements) from a movement that had articles but genuinely has no stored explanation yet (rarer - a prior Claude call failed, or `ANTHROPIC_API_KEY` wasn't set when it was last ingested), which stays `null`.
  - Trade-off: the threshold used to decide what's "explanation-worthy" at ingest time is fixed (2%), separate from the per-request `min_move_pct` the read endpoints accept - a request with a looser threshold can surface extra movements that were never candidates for a stored explanation. Also, a first-time backfill for a brand-new ticker can trigger many explanations in one ingestion run, since it's evaluating the ticker's whole default lookback window at once, not just one day.
- **The API itself runs in Docker too** (`api` service in `docker-compose.yml`, same `Dockerfile` as the ingestion worker with `command:` overridden to run Uvicorn instead). `app/` is bind-mounted over the image's copy and Uvicorn runs with `--reload`, so there's no "start the server" step separate from `docker compose up` and no loss of the fast edit/test loop you'd get running it locally. Trade-off: a change to `pyproject.toml` isn't picked up by the mount, since dependencies are installed at build time — that path needs an explicit `--build`.

## Submission questions

**Process, from start to finish:**
I started by re-reading the requirements and clarifying two unknowns that would shape the whole design: whether I had a news API key (no), and which LLM to use for chat (Claude). With those settled, I scaffolded a FastAPI project, then built bottom-up: price fetching + movement detection first (pure, testable logic), then the news layer behind an abstract interface so the "no API key" constraint wouldn't leak into the rest of the app, then the two required endpoints, then unit tests, then a manual run against a real ticker to sanity-check the output end-to-end. From there I iterated on making it closer to something you'd actually run: first replacing the in-memory cache with a Postgres-backed cache-aside layer (`docker compose` + a coverage-range table) so repeated queries stop re-hitting `yfinance`, then going further and fully decoupling ingestion from the API — pulling all `yfinance`/news calls into a separate `app/ingestion.py` module, adding a `POST /api/ingest/{ticker}` endpoint as the manual/onboarding path, and a separate scheduler container that refreshes tracked tickers on a timer. Then I brought the API itself into Docker as its own service with the code bind-mounted and Uvicorn running `--reload`, so the whole stack starts with one command and there's no separate "run the server" step to remember. Most recently, I moved movement explanation generation from the chat endpoint (live, on read) into ingestion itself - one batched Claude call per ingestion run, cached in Postgres - trading upfront LLM spend for a read path that's fast and doesn't depend on Claude being up. Verified each layer against a live database (and, once Docker became available in the sandbox, the real Compose stack, including a real end-to-end Claude call for the explanation feature) before moving to the next.

**Am I happy with the solution?**
Yes, given the time box and the constraint of no paid news API key. The architecture cleanly separates "detect movements" from "find news" from "answer questions," which means the biggest current weakness (news coverage for older movements) is isolated to one swappable class rather than baked into the whole system. The [Medium]/[Hard] news tiers (competitor/industry, macro/political) aren't implemented — with a real key I'd add a second `NewsProvider` and a lightweight relevance/classification step before extending the chat context.

**What would I do differently?**
I'd spend some of the setup time trying a couple of no-key news sources (e.g. RSS feeds, GDELT) to get partial historical coverage instead of relying solely on Yahoo's recent-news window. I'd also add streaming to the chat endpoint for a better UX on longer answers, replace the scheduler's plain sleep loop with a real job runner (APScheduler/Celery) if it needed per-ticker schedules or retry/backoff, and add a proper migration tool (Alembic) instead of the `create_all`-on-startup approach currently in `app/db.py`.

**Did I get stuck anywhere?**
The main snag was that `yfinance`'s pinned version in my first pass returned empty news lists — Yahoo had changed its response shape. Upgrading `yfinance` to the latest release and inspecting the raw response fixed it and revealed the nested `content` structure the current provider code parses. That's also what surfaced the "only ~10 recent articles" limitation, which then shaped the trade-off documented above instead of being a nasty surprise from a user later.
