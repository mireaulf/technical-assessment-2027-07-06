import logging
from datetime import date
from typing import Optional

from fastapi import FastAPI, HTTPException, Query

from app.analysis import get_ticker_analysis, list_industries, list_tracked_tickers
from app.chat import answer_chat
from app.db import init_db
from app.ingestion import ingest_ticker, log_active_news_providers
from app.models import ChatRequest, ChatResponse, TickerAnalysis, TrackedTicker
from app.stock_service import TickerNotFoundError, TickerNotIngestedError

# Not configured anywhere else in this process - without this, app.* loggers
# (e.g. app.ingestion's INFO-level startup log below) have no handler and
# are silently dropped, since the root logger's default level is WARNING.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

app = FastAPI(
    title="Stock Move Explainer",
    description="Explains major stock price movements using related news.",
)


@app.on_event("startup")
def on_startup():
    init_db()
    log_active_news_providers()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/tickers", response_model=list[TrackedTicker])
def get_tracked_tickers(
    industry: Optional[str] = Query(
        None,
        description=(
            "Case-insensitive substring filter on the ticker's Claude-classified industry "
            "(industry moves, requires EXA_API_KEY - see README). Tickers without a "
            "classification yet are excluded when this is set."
        ),
    ),
):
    """Every ticker that's been ingested at least once, with its data range."""
    return list_tracked_tickers(industry)


@app.get("/api/industries", response_model=list[str])
def get_industries():
    """Every distinct industry classified so far (industry moves).

    Not a fixed list - each entry was derived by Claude for some ticker
    (see app/news/classifier.py), so this only grows as tickers are
    ingested with EXA_API_KEY set. Use a value from here (or a
    substring of one) as `GET /api/tickers?industry=...`.
    """
    return list_industries()


@app.get("/api/tickers/{ticker}", response_model=TickerAnalysis)
def get_ticker(
    ticker: str,
    start_date: Optional[date] = Query(None, description="Defaults to 180 days before end_date"),
    end_date: Optional[date] = Query(None, description="Defaults to today"),
    min_move_pct: float = Query(2.0, ge=0, description="Minimum |daily % change| to flag as a movement"),
):
    """All stock price + news data for a ticker, filtered by date range and move threshold.

    Reads only from Postgres - see POST /api/ingest/{ticker} if the ticker
    hasn't been ingested yet.
    """
    try:
        return get_ticker_analysis(ticker, start_date, end_date, min_move_pct)
    except TickerNotIngestedError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    """Ask questions about a ticker's price movements and related news."""
    try:
        return answer_chat(request)
    except TickerNotIngestedError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/ingest/{ticker}")
def trigger_ingest(
    ticker: str,
    start_date: Optional[date] = Query(
        None, description="Defaults to the day after existing coverage, or a 180-day backfill for a new ticker"
    ),
    end_date: Optional[date] = Query(None, description="Defaults to today"),
):
    """Manually (re)fetch a ticker's prices and news from source and persist them.

    This is the only place the app calls out to yfinance on demand - the
    scheduler (app/scheduler.py) calls the same underlying function
    automatically for every ticker that's already been ingested at least
    once via this endpoint.
    """
    try:
        return ingest_ticker(ticker, start_date, end_date)
    except TickerNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
