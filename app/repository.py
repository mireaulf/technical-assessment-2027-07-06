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


def list_coverage(session: Session) -> list[TickerPriceCoverage]:
    """Every tracked ticker plus its ingested date range, for `GET /api/tickers`."""
    stmt = select(TickerPriceCoverage).order_by(TickerPriceCoverage.ticker)
    return list(session.scalars(stmt))


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
