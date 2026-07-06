from sqlalchemy import (
    JSON,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, future=True, expire_on_commit=False)

Base = declarative_base()


class PriceRow(Base):
    __tablename__ = "prices"

    ticker = Column(String, primary_key=True)
    date = Column(Date, primary_key=True)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Integer, nullable=False)
    pct_change = Column(Float, nullable=True)


class TickerPriceCoverage(Base):
    """Tracks the [min_date, max_date] range already fetched & stored per ticker.

    Historical daily bars never change once the market has closed, so once a
    range is covered here we never need to hit yfinance for it again.
    """

    __tablename__ = "ticker_price_coverage"

    ticker = Column(String, primary_key=True)
    min_date = Column(Date, nullable=False)
    max_date = Column(Date, nullable=False)


class ArticleRow(Base):
    __tablename__ = "articles"
    __table_args__ = (UniqueConstraint("ticker", "url", name="uq_articles_ticker_url"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String, nullable=False, index=True)
    title = Column(String, nullable=False)
    url = Column(String, nullable=False)
    source = Column(String, nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    summary = Column(String, nullable=True)
    category = Column(String, nullable=False, server_default="company")
    fetched_at = Column(DateTime(timezone=True), nullable=False)


class MovementExplanationRow(Base):
    """A pre-generated, cached explanation for one ticker's movement on one day.

    Generated at ingestion time (see app/explain.py), not on read, so the API
    never has to call Claude to answer "why did this move" and stays up even
    if Claude is down.
    """

    __tablename__ = "movement_explanations"

    ticker = Column(String, primary_key=True)
    date = Column(Date, primary_key=True)
    explanation = Column(String, nullable=False)
    model = Column(String, nullable=False)
    generated_at = Column(DateTime(timezone=True), nullable=False)


class TickerClassificationRow(Base):
    """Cached LLM classification of a ticker's industry + top competitors,
    used to drive industry moves news queries (see app/news/classifier.py).
    Classified once per ticker, not re-derived on every ingestion cycle.
    """

    __tablename__ = "ticker_classifications"

    ticker = Column(String, primary_key=True)
    industry = Column(String, nullable=True)
    competitors = Column(JSON, nullable=False, default=list)
    classified_at = Column(DateTime(timezone=True), nullable=False)


def init_db():
    Base.metadata.create_all(engine)
