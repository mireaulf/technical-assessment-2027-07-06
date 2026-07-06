from datetime import date, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf

from app.models import Article, Movement, PricePoint


class TickerNotFoundError(Exception):
    """Raised during ingestion when yfinance doesn't recognize the symbol at all."""


class TickerNotIngestedError(Exception):
    """Raised on the read path when a (possibly valid) ticker has no data in the database yet."""


def fetch_price_history(ticker: str, start_date: date, end_date: date) -> pd.DataFrame:
    # yfinance's `end` is exclusive, but callers expect an inclusive range.
    df = yf.Ticker(ticker).history(
        start=start_date.isoformat(),
        end=(end_date + timedelta(days=1)).isoformat(),
        auto_adjust=True,
    )
    if df.empty:
        raise TickerNotFoundError(f"No price data found for ticker '{ticker}'")
    df = df.reset_index()
    df["pct_change"] = df["Close"].pct_change() * 100
    return df


def to_price_points(df: pd.DataFrame) -> list[PricePoint]:
    points = []
    for _, row in df.iterrows():
        pct = row["pct_change"]
        points.append(
            PricePoint(
                date=row["Date"].date(),
                open=round(float(row["Open"]), 4),
                high=round(float(row["High"]), 4),
                low=round(float(row["Low"]), 4),
                close=round(float(row["Close"]), 4),
                volume=int(row["Volume"]),
                pct_change=round(float(pct), 4) if pd.notna(pct) else None,
            )
        )
    return points


def detect_movements(points: list[PricePoint], min_move_pct: float) -> list[Movement]:
    """Flag single-day moves whose |% change| >= min_move_pct.

    Uses the previous trading day's close as the baseline, matching how
    "daily move" is commonly reported for equities. Operates on `PricePoint`s
    rather than a raw yfinance frame so it works identically whether the
    points came from a fresh fetch or were read back from the Postgres cache.
    """
    movements = []
    for i in range(1, len(points)):
        pct = points[i].pct_change
        if pct is None or abs(pct) < min_move_pct:
            continue
        movements.append(
            Movement(
                date=points[i].date,
                close=points[i].close,
                prev_close=points[i - 1].close,
                pct_change=pct,
                direction="up" if pct > 0 else "down",
                articles=[],
            )
        )
    return movements


def attach_news_to_movements(
    movements: list[Movement],
    articles: list[Article],
    window_days: int = 2,
) -> None:
    """Attach articles published within `window_days` of a movement's date.

    Mutates `movements` in place. Because the default news provider only
    surfaces a handful of the most recent stories (see
    YFinanceNewsProvider), this will mostly populate movements that fall
    within the last few days - older movements are expected to end up
    with an empty `articles` list.
    """
    for movement in movements:
        for article in articles:
            if article.published_at is None:
                continue
            delta = abs((article.published_at.date() - movement.date).days)
            if delta <= window_days:
                movement.articles.append(article)
