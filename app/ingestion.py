import logging
from datetime import date, timedelta
from typing import Optional

from app.db import SessionLocal
from app.news.base import NewsProvider
from app.news.yfinance_provider import YFinanceNewsProvider
from app.repository import (
    extend_coverage,
    get_coverage,
    list_tracked_tickers,
    upsert_articles,
    upsert_prices,
)
from app.stock_service import fetch_price_history, to_price_points

logger = logging.getLogger(__name__)

DEFAULT_LOOKBACK_DAYS = 180
# Extra calendar days fetched before the true start so the first requested
# day's pct_change has a real previous close to compare against.
PCT_CHANGE_BUFFER_DAYS = 7

_news_provider: NewsProvider = YFinanceNewsProvider()


def ingest_ticker(ticker: str, start_date: Optional[date] = None, end_date: Optional[date] = None) -> dict:
    """Fetch prices (and best-effort news) for a ticker and persist them.

    This is the only function in the app that calls yfinance / the news
    provider - the REST API's read path (app/analysis.py) only ever queries
    Postgres. Called either by the scheduler (app/scheduler.py, no args -
    "bring this ticker up to date") or by the manual `POST /api/ingest`
    endpoint (optionally with an explicit range to backfill).
    """
    ticker = ticker.upper().strip()
    end_date = end_date or date.today()

    with SessionLocal() as session:
        coverage = get_coverage(session, ticker)
        if start_date is None:
            # No explicit range: extend an already-tracked ticker forward to
            # end_date, or backfill the default lookback for a brand-new one.
            start_date = coverage[1] + timedelta(days=1) if coverage else end_date - timedelta(
                days=DEFAULT_LOOKBACK_DAYS
            )

        fetch_start = start_date - timedelta(days=PCT_CHANGE_BUFFER_DAYS)
        fetch_end = end_date
        if coverage:
            fetch_start = min(fetch_start, coverage[0])
            fetch_end = max(fetch_end, coverage[1])

        df = fetch_price_history(ticker, fetch_start, fetch_end)  # TickerNotFoundError propagates
        points = to_price_points(df)
        upsert_prices(session, ticker, points)
        extend_coverage(session, ticker, min(p.date for p in points), max(p.date for p in points))

        articles = []
        try:
            articles = _news_provider.get_news(ticker)
            upsert_articles(session, ticker, articles)
        except Exception:
            logger.exception("News fetch failed for %s (prices were still ingested)", ticker)

        return {
            "ticker": ticker,
            "prices_ingested": len(points),
            "articles_ingested": len(articles),
            "coverage_start": min(p.date for p in points),
            "coverage_end": max(p.date for p in points),
        }


def ingest_tracked_tickers() -> list[dict]:
    """Refresh every ticker that's already being tracked.

    A ticker starts being tracked the first time `ingest_ticker` runs for
    it (normally via the manual endpoint) - this function never discovers
    new tickers on its own, it only keeps known ones up to date.
    """
    with SessionLocal() as session:
        tickers = list_tracked_tickers(session)

    results = []
    for ticker in tickers:
        try:
            results.append(ingest_ticker(ticker))
        except Exception:
            logger.exception("Scheduled ingestion failed for %s", ticker)
    return results
