import logging
from datetime import date, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.explain import generate_explanations
from app.models import Article
from app.news.base import NewsProvider
from app.news.classifier import classify_ticker
from app.news.composite_provider import CompositeNewsProvider
from app.news.newsapi_provider import NewsAPIProvider
from app.news.yfinance_provider import YFinanceNewsProvider
from app.repository import (
    extend_coverage,
    get_articles,
    get_classification,
    get_coverage,
    get_explanations,
    list_tracked_tickers,
    upsert_articles,
    upsert_classification,
    upsert_explanations,
    upsert_prices,
)
from app.stock_service import (
    attach_news_to_movements,
    detect_movements,
    fetch_price_history,
    to_price_points,
)

logger = logging.getLogger(__name__)

DEFAULT_LOOKBACK_DAYS = 180
# Extra calendar days fetched before the true start so the first requested
# day's pct_change has a real previous close to compare against.
PCT_CHANGE_BUFFER_DAYS = 7

# Threshold and news window used to decide which days get an explanation
# generated at ingestion time. Fixed (unlike the per-request min_move_pct on
# the read endpoints) since explanations are pre-computed once, not per
# request - a request with a looser threshold may reveal extra movements
# that were never candidates for a stored explanation.
EXPLANATION_MIN_MOVE_PCT = 2.0
EXPLANATION_NEWS_WINDOW_DAYS = 2

# Medium tier (industry/competitor news) only activates once a NewsAPI key
# is configured - without one, behavior is unchanged from the Easy tier.
_NEWSAPI_CONFIGURED = bool(settings.newsapi_api_key)


def _build_news_provider() -> NewsProvider:
    if not _NEWSAPI_CONFIGURED:
        return YFinanceNewsProvider()
    return CompositeNewsProvider([YFinanceNewsProvider(), NewsAPIProvider(settings.newsapi_api_key)])


_news_provider: NewsProvider = _build_news_provider()


def _get_or_classify(session: Session, ticker: str) -> tuple[Optional[str], list[str]]:
    """Industry + competitors for a ticker, classifying (and caching) via
    Claude on first use. Returns (None, []) if classification isn't
    available or fails - callers should treat that as "no Medium-tier
    context for this ticker" rather than an error.
    """
    row = get_classification(session, ticker)
    if row is not None:
        return row.industry, row.competitors

    if not settings.anthropic_api_key:
        return None, []

    classification = classify_ticker(ticker, settings.anthropic_api_key, settings.anthropic_model)
    if classification is None:
        return None, []

    upsert_classification(session, ticker, classification.industry, classification.competitors)
    return classification.industry, classification.competitors


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
            industry, competitors = (None, [])
            if _NEWSAPI_CONFIGURED:
                industry, competitors = _get_or_classify(session, ticker)
            articles = _news_provider.get_news(ticker, industry=industry, competitors=competitors)
            upsert_articles(session, ticker, articles)
        except Exception:
            logger.exception("News fetch failed for %s (prices were still ingested)", ticker)

        explanations_generated = 0
        try:
            explanations_generated = _generate_missing_explanations(session, ticker, points)
        except Exception:
            logger.exception("Explanation generation failed for %s (prices/news were still ingested)", ticker)

        return {
            "ticker": ticker,
            "prices_ingested": len(points),
            "articles_ingested": len(articles),
            "explanations_generated": explanations_generated,
            "coverage_start": min(p.date for p in points),
            "coverage_end": max(p.date for p in points),
        }


def _generate_missing_explanations(session, ticker: str, points: list) -> int:
    """Pre-generate and store explanations for newly-qualifying movements.

    Only covers the date range just fetched (not the ticker's whole
    history) - on a first-time backfill that can still mean many movements
    at once, which is the up-front cost this design deliberately accepts in
    exchange for the read path never depending on Claude.
    """
    movements = detect_movements(points, EXPLANATION_MIN_MOVE_PCT)
    if not movements:
        return 0

    already_explained = get_explanations(session, ticker, [m.date for m in movements])
    candidates = [m for m in movements if m.date not in already_explained]
    if not candidates:
        return 0

    buffer = timedelta(days=EXPLANATION_NEWS_WINDOW_DAYS)
    window_start = min(m.date for m in candidates) - buffer
    window_end = max(m.date for m in candidates) + buffer
    nearby_articles = [
        Article(
            title=r.title,
            url=r.url,
            source=r.source,
            published_at=r.published_at,
            summary=r.summary,
            category=r.category,
        )
        for r in get_articles(session, ticker, window_start, window_end)
    ]
    attach_news_to_movements(candidates, nearby_articles, window_days=EXPLANATION_NEWS_WINDOW_DAYS)

    explainable = [m for m in candidates if m.articles]
    if not explainable:
        return 0

    explanations = generate_explanations(ticker, explainable)
    if explanations:
        upsert_explanations(session, ticker, explanations, model=settings.anthropic_model)
    return len(explanations)


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
