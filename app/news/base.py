from abc import ABC, abstractmethod
from typing import Optional

from app.models import Article


class NewsProvider(ABC):
    """Pluggable source of company-specific news.

    Swap in a NewsAPI/GNews/Exa-backed implementation for broader
    (competitor/industry, macro/political) coverage without touching
    callers - they only depend on this interface.
    """

    @abstractmethod
    def get_news(self, ticker: str, company_name: Optional[str] = None) -> list[Article]:
        """Return recent news articles relevant to the given ticker."""
        raise NotImplementedError
