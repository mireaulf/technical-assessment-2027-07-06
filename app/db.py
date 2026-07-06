from sqlalchemy import (
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
    fetched_at = Column(DateTime(timezone=True), nullable=False)


def init_db():
    Base.metadata.create_all(engine)
