import logging

import app.ingestion as ingestion
from app.news.composite_provider import CompositeNewsProvider
from app.news.exa_provider import ExaProvider
from app.news.yfinance_provider import YFinanceNewsProvider


def test_log_active_news_providers_lists_single_provider(monkeypatch, caplog):
    monkeypatch.setattr(ingestion, "_news_provider", YFinanceNewsProvider())
    monkeypatch.setattr(ingestion, "_EXA_CONFIGURED", False)

    with caplog.at_level(logging.INFO, logger="app.ingestion"):
        ingestion.log_active_news_providers()

    assert "YFinanceNewsProvider" in caplog.text
    assert "ExaProvider" not in caplog.text
    assert "EXA_API_KEY" in caplog.text


def test_log_active_news_providers_lists_composite_providers(monkeypatch, caplog):
    monkeypatch.setattr(
        ingestion,
        "_news_provider",
        CompositeNewsProvider([YFinanceNewsProvider(), ExaProvider(api_key="fake-key")]),
    )
    monkeypatch.setattr(ingestion, "_EXA_CONFIGURED", True)

    with caplog.at_level(logging.INFO, logger="app.ingestion"):
        ingestion.log_active_news_providers()

    assert "YFinanceNewsProvider" in caplog.text
    assert "ExaProvider" in caplog.text
    assert "EXA_API_KEY" not in caplog.text
