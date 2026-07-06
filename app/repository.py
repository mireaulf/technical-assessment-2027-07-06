from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db import (
    ArticleRow,
    MovementExplanationRow,
    PriceRow,
    TickerClassificationRow,
    TickerPriceCoverage,
)
from app.models import Article, PricePoint


def get_coverage(session: Session, ticker: str) -> Optional[tuple[date, date]]:
    row = session.get(TickerPriceCoverage, ticker)
    return (row.min_date, row.max_date) if row else None


def list_tracked_tickers(session: Session) -> list[str]:
    """Every ticker that has been ingested at least once.

    This is the scheduler's worklist: a ticker starts being tracked the
    first time it's ingested (typically via the manual endpoint), and from
    then on the scheduler keeps it refreshed.
    """
    stmt = select(TickerPriceCoverage.ticker).order_by(TickerPriceCoverage.ticker)
    return list(session.scalars(stmt))


def list_coverage(
    session: Session, industry: Optional[str] = None
) -> list[tuple[TickerPriceCoverage, Optional[str]]]:
    """Every tracked ticker plus its ingested date range and (if classified)
    industry, for `GET /api/tickers`.

    `industry` does a case-insensitive substring match rather than an exact
    one - each ticker's industry label is generated independently by Claude
    (see app/news/classifier.py), not drawn from a fixed taxonomy, so two
    related tickers may be worded slightly differently (e.g. "Semiconductors"
    vs. "Semiconductor Manufacturing"). Tickers with no classification yet
    (EXA_API_KEY unset, or not yet ingested) are excluded when filtering.
    """
    stmt = (
        select(TickerPriceCoverage, TickerClassificationRow.industry)
        .join(
            TickerClassificationRow,
            TickerClassificationRow.ticker == TickerPriceCoverage.ticker,
            isouter=True,
        )
        .order_by(TickerPriceCoverage.ticker)
    )
    if industry:
        stmt = stmt.where(TickerClassificationRow.industry.ilike(f"%{industry}%"))
    return [tuple(row) for row in session.execute(stmt).all()]


def extend_coverage(session: Session, ticker: str, start: date, end: date) -> None:
    existing = session.get(TickerPriceCoverage, ticker)
    if existing:
        existing.min_date = min(existing.min_date, start)
        existing.max_date = max(existing.max_date, end)
    else:
        session.add(TickerPriceCoverage(ticker=ticker, min_date=start, max_date=end))
    session.commit()


def upsert_prices(session: Session, ticker: str, points: list[PricePoint]) -> None:
    if not points:
        return
    rows = [
        {
            "ticker": ticker,
            "date": p.date,
            "open": p.open,
            "high": p.high,
            "low": p.low,
            "close": p.close,
            "volume": p.volume,
            "pct_change": p.pct_change,
        }
        for p in points
    ]
    stmt = pg_insert(PriceRow).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["ticker", "date"],
        set_={
            "open": stmt.excluded.open,
            "high": stmt.excluded.high,
            "low": stmt.excluded.low,
            "close": stmt.excluded.close,
            "volume": stmt.excluded.volume,
            "pct_change": stmt.excluded.pct_change,
        },
    )
    session.execute(stmt)
    session.commit()


def get_prices(session: Session, ticker: str, start: date, end: date) -> list[PriceRow]:
    stmt = (
        select(PriceRow)
        .where(PriceRow.ticker == ticker, PriceRow.date >= start, PriceRow.date <= end)
        .order_by(PriceRow.date)
    )
    return list(session.scalars(stmt))


def upsert_articles(session: Session, ticker: str, articles: list[Article]) -> None:
    if not articles:
        return
    now = datetime.now(timezone.utc)
    rows = [
        {
            "ticker": ticker,
            "title": a.title,
            "url": a.url,
            "source": a.source,
            "published_at": a.published_at,
            "summary": a.summary,
            "category": a.category,
            "fetched_at": now,
        }
        for a in articles
        if a.url
    ]
    if not rows:
        return
    stmt = pg_insert(ArticleRow).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["ticker", "url"],
        set_={
            "title": stmt.excluded.title,
            "source": stmt.excluded.source,
            "published_at": stmt.excluded.published_at,
            "summary": stmt.excluded.summary,
            "category": stmt.excluded.category,
            "fetched_at": stmt.excluded.fetched_at,
        },
    )
    session.execute(stmt)
    session.commit()


def get_articles(session: Session, ticker: str, start: date, end: date) -> list[ArticleRow]:
    stmt = (
        select(ArticleRow)
        .where(
            ArticleRow.ticker == ticker,
            ArticleRow.published_at >= datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc),
            ArticleRow.published_at <= datetime.combine(end, datetime.max.time(), tzinfo=timezone.utc),
        )
        .order_by(ArticleRow.published_at)
    )
    return list(session.scalars(stmt))


def get_explanations(session: Session, ticker: str, dates: list[date]) -> dict[date, str]:
    if not dates:
        return {}
    stmt = select(MovementExplanationRow).where(
        MovementExplanationRow.ticker == ticker, MovementExplanationRow.date.in_(dates)
    )
    return {row.date: row.explanation for row in session.scalars(stmt)}


def upsert_explanations(session: Session, ticker: str, explanations: dict[date, str], model: str) -> None:
    if not explanations:
        return
    now = datetime.now(timezone.utc)
    rows = [
        {"ticker": ticker, "date": d, "explanation": text, "model": model, "generated_at": now}
        for d, text in explanations.items()
    ]
    stmt = pg_insert(MovementExplanationRow).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["ticker", "date"],
        set_={"explanation": stmt.excluded.explanation, "model": stmt.excluded.model, "generated_at": stmt.excluded.generated_at},
    )
    session.execute(stmt)
    session.commit()


def get_classification(session: Session, ticker: str) -> Optional[TickerClassificationRow]:
    return session.get(TickerClassificationRow, ticker)


def list_classified_industries(session: Session) -> list[str]:
    """Every distinct industry classified so far, for `GET /api/industries`.

    Not a fixed taxonomy - each entry was independently derived by Claude
    for some ticker (see app/news/classifier.py), so this list only grows
    as more tickers get ingested with EXA_API_KEY set.
    """
    stmt = (
        select(TickerClassificationRow.industry)
        .where(TickerClassificationRow.industry.isnot(None))
        .distinct()
        .order_by(TickerClassificationRow.industry)
    )
    return list(session.scalars(stmt))


def upsert_classification(session: Session, ticker: str, industry: str, competitors: list[str]) -> None:
    now = datetime.now(timezone.utc)
    row = session.get(TickerClassificationRow, ticker)
    if row:
        row.industry = industry
        row.competitors = competitors
        row.classified_at = now
    else:
        session.add(
            TickerClassificationRow(ticker=ticker, industry=industry, competitors=competitors, classified_at=now)
        )
    session.commit()
