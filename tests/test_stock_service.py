from datetime import date, timedelta

import pandas as pd
import pytest

from app.models import Article
from app.stock_service import attach_news_to_movements, detect_movements, to_price_points


def make_df(closes):
    start = date(2026, 1, 1)
    dates = [pd.Timestamp(start + timedelta(days=i)) for i in range(len(closes))]
    df = pd.DataFrame(
        {
            "Date": dates,
            "Open": closes,
            "High": [c * 1.01 for c in closes],
            "Low": [c * 0.99 for c in closes],
            "Close": closes,
            "Volume": [1_000_000] * len(closes),
        }
    )
    df["pct_change"] = df["Close"].pct_change() * 100
    return df


def make_points(closes):
    return to_price_points(make_df(closes))


def test_detect_movements_flags_moves_above_threshold():
    # day 1->2: +5%, day 2->3: +0.5% (below threshold), day 3->4: -10%
    points = make_points([100, 105, 105.525, 94.9725])
    movements = detect_movements(points, min_move_pct=2.0)

    assert len(movements) == 2
    assert movements[0].direction == "up"
    assert movements[0].pct_change == pytest.approx(5.0, abs=0.01)
    assert movements[1].direction == "down"
    assert movements[1].pct_change == pytest.approx(-10.0, abs=0.01)


def test_detect_movements_respects_custom_threshold():
    points = make_points([100, 101, 103])
    assert detect_movements(points, min_move_pct=2.0) == []
    movements = detect_movements(points, min_move_pct=1.0)
    assert len(movements) == 2


def test_detect_movements_empty_when_no_data():
    points = make_points([100])
    assert detect_movements(points, min_move_pct=2.0) == []


def test_to_price_points_first_row_has_no_pct_change():
    points = make_points([100, 105])
    assert points[0].pct_change is None
    assert points[1].pct_change == pytest.approx(5.0, abs=0.01)


def test_attach_news_to_movements_matches_within_window():
    points = make_points([100, 105])
    movements = detect_movements(points, min_move_pct=2.0)
    movement_date = movements[0].date

    close_article = Article(
        title="Big news",
        url="http://example.com/1",
        published_at=pd.Timestamp(movement_date).to_pydatetime(),
    )
    far_article = Article(
        title="Unrelated news",
        url="http://example.com/2",
        published_at=pd.Timestamp(movement_date + timedelta(days=30)).to_pydatetime(),
    )

    attach_news_to_movements(movements, [close_article, far_article], window_days=2)

    assert len(movements[0].articles) == 1
    assert movements[0].articles[0].title == "Big news"


def test_attach_news_to_movements_ignores_articles_without_date():
    points = make_points([100, 105])
    movements = detect_movements(points, min_move_pct=2.0)
    undated = Article(title="No date", url="http://example.com/3", published_at=None)

    attach_news_to_movements(movements, [undated])

    assert movements[0].articles == []
