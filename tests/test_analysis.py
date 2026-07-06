from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text

from app.analysis import NO_NEWS_COVERAGE_MESSAGE, get_ticker_analysis
from app.db import Base, SessionLocal, engine
from app.models import Article, PricePoint
from app.repository import extend_coverage, upsert_articles, upsert_explanations, upsert_prices

TEST_TICKER = "TEST_ANALYSIS_TICKER"


@pytest.fixture
def session():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("Postgres not reachable - run `docker compose up` to enable these tests")

    Base.metadata.create_all(engine)
    with SessionLocal() as s:
        yield s
        for table in ("articles", "prices", "ticker_price_coverage", "movement_explanations"):
            s.execute(text(f"DELETE FROM {table} WHERE ticker = :t"), {"t": TEST_TICKER})
        s.commit()


def _seed_prices(session):
    points = [
        PricePoint(date=date(2026, 3, 1), open=100, high=101, low=99, close=100, volume=1000, pct_change=None),
        PricePoint(date=date(2026, 3, 2), open=100, high=106, low=99, close=105, volume=1000, pct_change=5.0),
        PricePoint(date=date(2026, 3, 3), open=105, high=112, low=104, close=110, volume=1000, pct_change=4.76),
    ]
    upsert_prices(session, TEST_TICKER, points)
    extend_coverage(session, TEST_TICKER, date(2026, 3, 1), date(2026, 3, 3))
    session.commit()


def test_movement_without_articles_gets_no_news_message(session):
    _seed_prices(session)

    analysis = get_ticker_analysis(TEST_TICKER, date(2026, 3, 1), date(2026, 3, 3), min_move_pct=2.0)

    movement = next(m for m in analysis.movements if m.date == date(2026, 3, 2))
    assert movement.articles == []
    assert movement.explanation == NO_NEWS_COVERAGE_MESSAGE


def test_movement_with_articles_but_no_stored_explanation_stays_null(session):
    _seed_prices(session)
    upsert_articles(
        session,
        TEST_TICKER,
        [
            Article(
                title="Some headline",
                url="http://example.com/analysis-test",
                published_at=datetime(2026, 3, 2, tzinfo=timezone.utc),
            )
        ],
    )

    analysis = get_ticker_analysis(TEST_TICKER, date(2026, 3, 1), date(2026, 3, 3), min_move_pct=2.0)

    movement = next(m for m in analysis.movements if m.date == date(2026, 3, 2))
    assert len(movement.articles) == 1
    assert movement.explanation is None


def test_movement_with_stored_explanation_returns_it_verbatim(session):
    _seed_prices(session)
    upsert_explanations(session, TEST_TICKER, {date(2026, 3, 2): "Moved on earnings."}, model="claude-test-model")

    analysis = get_ticker_analysis(TEST_TICKER, date(2026, 3, 1), date(2026, 3, 3), min_move_pct=2.0)

    movement = next(m for m in analysis.movements if m.date == date(2026, 3, 2))
    assert movement.explanation == "Moved on earnings."
