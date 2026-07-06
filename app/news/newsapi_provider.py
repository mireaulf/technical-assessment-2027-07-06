import logging
from datetime import datetime
from typing import Optional

import httpx

from app.models import Article
from app.news.base import NewsProvider

logger = logging.getLogger(__name__)

NEWSAPI_URL = "https://newsapi.org/v2/everything"
REQUEST_TIMEOUT_SECONDS = 10
PAGE_SIZE = 10


class NewsAPIProvider(NewsProvider):
    """Broader (Medium-tier) news via NewsAPI, driven by an `industry` and
    `competitors` classification (see app/news/classifier.py) rather than
    the ticker itself - this provider is meant to run alongside
    `YFinanceNewsProvider`, not replace it.
    """

    def __init__(self, api_key: str):
        self.api_key = api_key

    def get_news(
        self,
        ticker: str,
        company_name: Optional[str] = None,
        industry: Optional[str] = None,
        competitors: Optional[list[str]] = None,
    ) -> list[Article]:
        articles = []
        if industry:
            articles.extend(self._search(industry, category="industry"))
        for name in competitors or []:
            articles.extend(self._search(name, category="competitor"))
        return articles

    def _search(self, query: str, category: str) -> list[Article]:
        try:
            response = httpx.get(
                NEWSAPI_URL,
                params={
                    "q": query,
                    "language": "en",
                    "sortBy": "publishedAt",
                    "pageSize": PAGE_SIZE,
                    "apiKey": self.api_key,
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            if response.status_code != 200:
                logger.warning("NewsAPI query %r failed: HTTP %s", query, response.status_code)
                return []
            data = response.json()
        except Exception:
            logger.exception("NewsAPI query %r failed", query)
            return []

        articles = []
        for item in data.get("articles", []):
            title = item.get("title")
            url = item.get("url")
            if not title or not url:
                continue
            published_at = None
            pub_date = item.get("publishedAt")
            if pub_date:
                try:
                    published_at = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
                except ValueError:
                    published_at = None
            articles.append(
                Article(
                    title=title,
                    url=url,
                    source=(item.get("source") or {}).get("name"),
                    published_at=published_at,
                    summary=item.get("description") or None,
                    category=category,
                )
            )
        return articles
