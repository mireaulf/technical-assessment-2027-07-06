from datetime import date, timedelta
from typing import Optional

from app.db import SessionLocal
from app.models import Article, PricePoint, TickerAnalysis, TrackedTicker
from app.repository import get_articles, get_coverage, get_prices, list_coverage
from app.stock_service import TickerNotIngestedError, attach_news_to_movements, detect_movements

DEFAULT_LOOKBACK_DAYS = 180
NEWS_WINDOW_DAYS = 2


def _row_to_point(row) -> PricePoint:
    return PricePoint(
        date=row.date,
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        volume=row.volume,
        pct_change=row.pct_change,
    )


def _row_to_article(row) -> Article:
    return Article(
        title=row.title,
        url=row.url,
        source=row.source,
        published_at=row.published_at,
        summary=row.summary,
    )


def get_ticker_analysis(
    ticker: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    min_move_pct: float = 2.0,
) -> TickerAnalysis:
    """Read-only: serves entirely from Postgres, never calls yfinance.

    Ingestion (fetching from yfinance/news and persisting it) is a separate
    concern - see app/ingestion.py, run on a schedule (app/scheduler.py) or
    on demand via `POST /api/ingest/{ticker}`. If a ticker has never been
    ingested, this raises `TickerNotIngestedError` rather than fetching it
    inline, so the API's request path has no external dependencies.
    """
    ticker = ticker.upper().strip()
    end_date = end_date or date.today()
    start_date = start_date or (end_date - timedelta(days=DEFAULT_LOOKBACK_DAYS))

    with SessionLocal() as session:
        coverage = get_coverage(session, ticker)
        if coverage is None:
            raise TickerNotIngestedError(
                f"'{ticker}' has not been ingested yet. "
                f"POST /api/ingest/{ticker} to fetch it, then retry."
            )

        price_rows = get_prices(session, ticker, start_date, end_date)
        prices = [_row_to_point(r) for r in price_rows]
        movements = detect_movements(prices, min_move_pct)

        buffer = timedelta(days=NEWS_WINDOW_DAYS)
        article_rows = get_articles(session, ticker, start_date - buffer, end_date + buffer)
        articles = [_row_to_article(r) for r in article_rows]
        attach_news_to_movements(movements, articles, window_days=NEWS_WINDOW_DAYS)

        return TickerAnalysis(
            ticker=ticker,
            start_date=start_date,
            end_date=end_date,
            min_move_pct=min_move_pct,
            prices=prices,
            movements=movements,
            data_coverage_start=coverage[0],
            data_coverage_end=coverage[1],
        )


def list_tracked_tickers() -> list[TrackedTicker]:
    """Every ticker that's been ingested at least once, with its data range."""
    with SessionLocal() as session:
        return [
            TrackedTicker(ticker=row.ticker, data_coverage_start=row.min_date, data_coverage_end=row.max_date)
            for row in list_coverage(session)
        ]
