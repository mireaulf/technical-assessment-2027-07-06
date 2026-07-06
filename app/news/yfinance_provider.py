from datetime import datetime
from typing import Optional

import yfinance as yf

from app.models import Article
from app.news.base import NewsProvider


class YFinanceNewsProvider(NewsProvider):
    """Company-specific news via Yahoo Finance (no API key required).

    Trade-off: Yahoo's feed only exposes a rolling window of the most
    recent ~10-20 stories per ticker, not an arbitrary historical
    date-range archive. This means it aligns well with recent price
    movements but will return no articles for older movements. A paid
    provider (NewsAPI/GNews/Exa) plugged in behind `NewsProvider` would
    remove that limitation.
    """

    def get_news(
        self,
        ticker: str,
        company_name: Optional[str] = None,
        industry: Optional[str] = None,
        competitors: Optional[list[str]] = None,
    ) -> list[Article]:
        raw = yf.Ticker(ticker).news or []
        articles = []
        for item in raw:
            content = item.get("content", item)
            title = content.get("title")
            if not title:
                continue
            url = (
                content.get("canonicalUrl", {}).get("url")
                or content.get("clickThroughUrl", {}).get("url")
                or ""
            )
            published_at = None
            pub_date = content.get("pubDate")
            if pub_date:
                try:
                    published_at = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
                except ValueError:
                    published_at = None
            articles.append(
                Article(
                    title=title,
                    url=url,
                    source=content.get("provider", {}).get("displayName"),
                    published_at=published_at,
                    summary=content.get("summary") or None,
                )
            )
        return articles
