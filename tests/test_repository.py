from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text

from app.db import Base, SessionLocal, engine
from app.models import Article, PricePoint
from app.repository import (
    extend_coverage,
    get_articles,
    get_coverage,
    get_explanations,
    get_prices,
    list_tracked_tickers,
    upsert_articles,
    upsert_explanations,
    upsert_prices,
)

TEST_TICKER = "TEST_TICKER"


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
        # Clean up rows created by this ticker so re-runs stay idempotent.
        for table in ("articles", "prices", "ticker_price_coverage", "movement_explanations"):
            s.execute(text(f"DELETE FROM {table} WHERE ticker = :t"), {"t": TEST_TICKER})
        s.commit()


def test_upsert_and_get_prices_roundtrip(session):
    points = [
        PricePoint(date=date(2026, 1, 2), open=10, high=11, low=9, close=10.5, volume=100, pct_change=None),
        PricePoint(date=date(2026, 1, 3), open=10.5, high=12, low=10, close=11.5, volume=200, pct_change=9.52),
    ]
    upsert_prices(session, TEST_TICKER, points)

    rows = get_prices(session, TEST_TICKER, date(2026, 1, 1), date(2026, 1, 31))
    assert [r.date for r in rows] == [date(2026, 1, 2), date(2026, 1, 3)]
    assert rows[1].pct_change == pytest.approx(9.52)


def test_upsert_prices_overwrites_on_conflict(session):
    p = PricePoint(date=date(2026, 1, 2), open=10, high=11, low=9, close=10.5, volume=100, pct_change=None)
    upsert_prices(session, TEST_TICKER, [p])

    updated = PricePoint(date=date(2026, 1, 2), open=10, high=11, low=9, close=99.9, volume=100, pct_change=1.0)
    upsert_prices(session, TEST_TICKER, [updated])

    rows = get_prices(session, TEST_TICKER, date(2026, 1, 1), date(2026, 1, 31))
    assert len(rows) == 1
    assert rows[0].close == 99.9


def test_list_tracked_tickers_includes_ticker_once_covered(session):
    assert TEST_TICKER not in list_tracked_tickers(session)

    extend_coverage(session, TEST_TICKER, date(2026, 1, 1), date(2026, 1, 31))

    assert TEST_TICKER in list_tracked_tickers(session)


def test_extend_coverage_unions_ranges(session):
    extend_coverage(session, TEST_TICKER, date(2026, 2, 1), date(2026, 2, 28))
    assert get_coverage(session, TEST_TICKER) == (date(2026, 2, 1), date(2026, 2, 28))

    extend_coverage(session, TEST_TICKER, date(2026, 1, 15), date(2026, 2, 10))
    assert get_coverage(session, TEST_TICKER) == (date(2026, 1, 15), date(2026, 2, 28))


def test_upsert_articles_dedupes_by_url(session):
    article = Article(
        title="Some headline",
        url="http://example.com/story-1",
        published_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    upsert_articles(session, TEST_TICKER, [article])
    upsert_articles(session, TEST_TICKER, [article])  # duplicate, should be a no-op

    rows = get_articles(session, TEST_TICKER, date(2026, 1, 1), date(2026, 1, 31))
    assert len(rows) == 1


def test_upsert_articles_overwrites_on_conflict(session):
    original = Article(
        title="Original headline",
        url="http://example.com/story-2",
        published_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        summary="Old summary",
        category="company",
    )
    upsert_articles(session, TEST_TICKER, [original])

    updated = Article(
        title="Updated headline",
        url="http://example.com/story-2",
        published_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        summary="New summary",
        category="industry",
    )
    upsert_articles(session, TEST_TICKER, [updated])

    rows = get_articles(session, TEST_TICKER, date(2026, 1, 1), date(2026, 1, 31))
    assert len(rows) == 1
    assert rows[0].title == "Updated headline"
    assert rows[0].summary == "New summary"
    assert rows[0].category == "industry"


def test_get_articles_filters_by_published_date(session):
    in_range = Article(
        title="In range",
        url="http://example.com/in-range",
        published_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
    )
    out_of_range = Article(
        title="Out of range",
        url="http://example.com/out-of-range",
        published_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    upsert_articles(session, TEST_TICKER, [in_range, out_of_range])

    rows = get_articles(session, TEST_TICKER, date(2026, 1, 1), date(2026, 1, 31))
    assert [r.title for r in rows] == ["In range"]


def test_upsert_and_get_explanations_roundtrip(session):
    upsert_explanations(
        session,
        TEST_TICKER,
        {date(2026, 1, 2): "Moved on earnings.", date(2026, 1, 3): "Moved on a lawsuit."},
        model="claude-test-model",
    )

    result = get_explanations(session, TEST_TICKER, [date(2026, 1, 2), date(2026, 1, 3), date(2026, 1, 4)])

    assert result == {
        date(2026, 1, 2): "Moved on earnings.",
        date(2026, 1, 3): "Moved on a lawsuit.",
    }


def test_upsert_explanations_overwrites_on_conflict(session):
    upsert_explanations(session, TEST_TICKER, {date(2026, 1, 2): "First draft."}, model="claude-test-model")
    upsert_explanations(session, TEST_TICKER, {date(2026, 1, 2): "Revised."}, model="claude-test-model-2")

    result = get_explanations(session, TEST_TICKER, [date(2026, 1, 2)])

    assert result == {date(2026, 1, 2): "Revised."}
