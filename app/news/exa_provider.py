import logging
from datetime import datetime
from typing import Optional

import httpx

from app.models import Article
from app.news.base import NewsProvider

logger = logging.getLogger(__name__)

EXA_SEARCH_URL = "https://api.exa.ai/search"
REQUEST_TIMEOUT_SECONDS = 10
NUM_RESULTS = 10


class ExaProvider(NewsProvider):
    """Broader (industry moves) news via Exa's neural search API, driven by
    an `industry` and `competitors` classification (see
    app/news/classifier.py) rather than the ticker itself - this provider
    is meant to run alongside `YFinanceNewsProvider`, not replace it.

    Exa over a keyword news API here: `industry`/`competitors` are
    loosely-specified terms handed off from an LLM classification (e.g. an
    industry label, a company name), not exact phrases to match - Exa's
    neural search handles that better than literal keyword matching, and
    its content extraction returns clean article text/summaries suited to
    grounding an LLM rather than just a title + link.
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
            articles.extend(self._search(f"Recent news about the {industry} industry", category="industry"))
        for name in competitors or []:
            articles.extend(self._search(f"Recent news about {name}", category="competitor"))
        return articles

    def _search(self, query: str, category: str) -> list[Article]:
        try:
            response = httpx.post(
                EXA_SEARCH_URL,
                headers={"x-api-key": self.api_key, "Content-Type": "application/json"},
                json={
                    "query": query,
                    "type": "neural",
                    "category": "news",
                    "numResults": NUM_RESULTS,
                    "contents": {"summary": True},
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            if response.status_code != 200:
                logger.warning("Exa query %r failed: HTTP %s", query, response.status_code)
                return []
            data = response.json()
        except Exception:
            logger.exception("Exa query %r failed", query)
            return []

        articles = []
        for item in data.get("results", []):
            title = item.get("title")
            url = item.get("url")
            if not title or not url:
                continue
            published_at = None
            pub_date = item.get("publishedDate")
            if pub_date:
                try:
                    published_at = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
                except ValueError:
                    published_at = None
            articles.append(
                Article(
                    title=title,
                    url=url,
                    source=item.get("author"),
                    published_at=published_at,
                    summary=item.get("summary") or None,
                    category=category,
                )
            )
        return articles
