from datetime import date as Date
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class PricePoint(BaseModel):
    date: Date
    open: float
    high: float
    low: float
    close: float
    volume: int
    pct_change: Optional[float] = None


class Article(BaseModel):
    title: str
    url: str
    source: Optional[str] = None
    published_at: Optional[datetime] = None
    summary: Optional[str] = None
    # "company" (Easy tier), "industry" or "competitor" (Medium tier).
    category: str = "company"


class TickerClassification(BaseModel):
    """Best-effort LLM classification of a ticker, used to drive Medium-tier
    (industry/competitor) news queries. See app/news/classifier.py."""

    industry: str
    competitors: list[str] = []


class Movement(BaseModel):
    date: Date
    close: float
    prev_close: float
    pct_change: float
    direction: str  # "up" | "down"
    articles: list[Article] = []
    # Pre-generated at ingestion time (see app/explain.py) when articles were
    # available - None if never generated (no nearby news yet, below the
    # ingestion-time explanation threshold, or ANTHROPIC_API_KEY unset).
    explanation: Optional[str] = None


class TickerAnalysis(BaseModel):
    ticker: str
    start_date: Date
    end_date: Date
    min_move_pct: float
    prices: list[PricePoint]
    movements: list[Movement]
    # What's actually been ingested for this ticker, which may not fully
    # cover [start_date, end_date] if it hasn't been (re)ingested recently.
    data_coverage_start: Date
    data_coverage_end: Date


class TrackedTicker(BaseModel):
    ticker: str
    data_coverage_start: Date
    data_coverage_end: Date
    # Claude-classified industry (see app/news/classifier.py) - only present
    # for tickers ingested with NEWSAPI_API_KEY set; None otherwise.
    industry: Optional[str] = None


class ChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    ticker: str
    message: str
    start_date: Optional[Date] = None
    end_date: Optional[Date] = None
    min_move_pct: float = 2.0
    history: list[ChatMessage] = []


class ChatResponse(BaseModel):
    reply: str
    ticker: str
    movements_considered: int
