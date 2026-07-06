import logging
from typing import Optional

from app.models import Article
from app.news.base import NewsProvider

logger = logging.getLogger(__name__)


class CompositeNewsProvider(NewsProvider):
    """Fans a single `get_news` call out to several providers and merges
    the results, deduped by URL. One provider failing doesn't drop the
    others' articles - e.g. a NewsAPI outage shouldn't take down the
    yfinance-backed company news alongside it.
    """

    def __init__(self, providers: list[NewsProvider]):
        self.providers = providers

    def get_news(
        self,
        ticker: str,
        company_name: Optional[str] = None,
        industry: Optional[str] = None,
        competitors: Optional[list[str]] = None,
    ) -> list[Article]:
        seen_urls: set[str] = set()
        merged: list[Article] = []
        for provider in self.providers:
            try:
                articles = provider.get_news(
                    ticker, company_name=company_name, industry=industry, competitors=competitors
                )
            except Exception:
                logger.exception("News provider %s failed for %s", type(provider).__name__, ticker)
                continue
            for article in articles:
                if article.url in seen_urls:
                    continue
                seen_urls.add(article.url)
                merged.append(article)
        return merged
