import json
from datetime import datetime, timezone

import pytest

from app.models import Article
from app.news.classifier import classify_ticker
from app.news.composite_provider import CompositeNewsProvider
from app.news.newsapi_provider import NewsAPIProvider


class _FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeAnthropicResponse:
    def __init__(self, text):
        self.content = [_FakeTextBlock(text)]


class _FakeAnthropicMessages:
    def __init__(self, text):
        self._text = text
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeAnthropicResponse(self._text)


class _FakeAnthropicClient:
    def __init__(self, text):
        self.messages = _FakeAnthropicMessages(text)


def test_classify_ticker_parses_valid_json(monkeypatch):
    payload = json.dumps({"industry": "Semiconductors", "competitors": ["AMD", "Intel", "Qualcomm", "Extra"]})
    monkeypatch.setattr(
        "app.news.classifier.anthropic.Anthropic", lambda api_key: _FakeAnthropicClient(payload)
    )
    monkeypatch.setattr("app.news.classifier.yf.Ticker", lambda ticker: type("T", (), {"info": {}})())

    result = classify_ticker("NVDA", api_key="fake-key", model="fake-model")

    assert result is not None
    assert result.industry == "Semiconductors"
    assert result.competitors == ["AMD", "Intel", "Qualcomm"]  # capped at 3


def test_classify_ticker_returns_none_on_malformed_json(monkeypatch):
    monkeypatch.setattr(
        "app.news.classifier.anthropic.Anthropic", lambda api_key: _FakeAnthropicClient("not json")
    )
    monkeypatch.setattr("app.news.classifier.yf.Ticker", lambda ticker: type("T", (), {"info": {}})())

    assert classify_ticker("NVDA", api_key="fake-key", model="fake-model") is None


def test_classify_ticker_returns_none_without_api_key():
    assert classify_ticker("NVDA", api_key="", model="fake-model") is None


def test_classify_ticker_prompts_reuse_of_existing_industries(monkeypatch):
    payload = json.dumps({"industry": "Semiconductors", "competitors": []})
    client = _FakeAnthropicClient(payload)
    monkeypatch.setattr("app.news.classifier.anthropic.Anthropic", lambda api_key: client)
    monkeypatch.setattr("app.news.classifier.yf.Ticker", lambda ticker: type("T", (), {"info": {}})())

    classify_ticker("NVDA", api_key="fake-key", model="fake-model", existing_industries=["Semiconductors", "Retail"])

    prompt = client.messages.last_kwargs["messages"][0]["content"]
    assert "Semiconductors" in prompt
    assert "Retail" in prompt
    assert "already in use" in prompt


def test_classify_ticker_omits_reuse_hint_without_existing_industries(monkeypatch):
    payload = json.dumps({"industry": "Semiconductors", "competitors": []})
    client = _FakeAnthropicClient(payload)
    monkeypatch.setattr("app.news.classifier.anthropic.Anthropic", lambda api_key: client)
    monkeypatch.setattr("app.news.classifier.yf.Ticker", lambda ticker: type("T", (), {"info": {}})())

    classify_ticker("NVDA", api_key="fake-key", model="fake-model")

    prompt = client.messages.last_kwargs["messages"][0]["content"]
    assert "already in use" not in prompt


class _FakeHTTPResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _newsapi_payload(titles_and_urls):
    return {
        "articles": [
            {
                "title": title,
                "url": url,
                "source": {"name": "Some Source"},
                "publishedAt": "2026-01-02T00:00:00Z",
                "description": "summary",
            }
            for title, url in titles_and_urls
        ]
    }


def test_newsapi_provider_tags_industry_and_competitor_queries(monkeypatch):
    calls = []

    def fake_get(url, params, timeout):
        calls.append(params["q"])
        if params["q"] == "Semiconductors":
            return _FakeHTTPResponse(200, _newsapi_payload([("Industry story", "http://example.com/industry")]))
        return _FakeHTTPResponse(200, _newsapi_payload([(f"{params['q']} story", f"http://example.com/{params['q']}")]))

    monkeypatch.setattr("app.news.newsapi_provider.httpx.get", fake_get)

    provider = NewsAPIProvider(api_key="fake-key")
    articles = provider.get_news("NVDA", industry="Semiconductors", competitors=["AMD", "Intel"])

    assert set(calls) == {"Semiconductors", "AMD", "Intel"}
    by_category = {a.category for a in articles}
    assert by_category == {"industry", "competitor"}
    assert len(articles) == 3


def test_newsapi_provider_returns_empty_on_failure(monkeypatch):
    monkeypatch.setattr(
        "app.news.newsapi_provider.httpx.get", lambda url, params, timeout: _FakeHTTPResponse(500, {})
    )

    provider = NewsAPIProvider(api_key="fake-key")
    assert provider.get_news("NVDA", industry="Semiconductors") == []

    def raising_get(*args, **kwargs):
        raise ConnectionError("boom")

    monkeypatch.setattr("app.news.newsapi_provider.httpx.get", raising_get)
    assert provider.get_news("NVDA", industry="Semiconductors") == []


class _StubProvider:
    def __init__(self, articles=None, error=False):
        self._articles = articles or []
        self._error = error

    def get_news(self, ticker, company_name=None, industry=None, competitors=None):
        if self._error:
            raise RuntimeError("provider failed")
        return self._articles


def _article(url, category="company"):
    return Article(
        title=f"Story {url}",
        url=url,
        published_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        category=category,
    )


def test_composite_provider_merges_and_dedupes_by_url():
    provider_a = _StubProvider([_article("http://example.com/1"), _article("http://example.com/2")])
    provider_b = _StubProvider([_article("http://example.com/2"), _article("http://example.com/3")])

    composite = CompositeNewsProvider([provider_a, provider_b])
    articles = composite.get_news("NVDA")

    assert sorted(a.url for a in articles) == [
        "http://example.com/1",
        "http://example.com/2",
        "http://example.com/3",
    ]


def test_composite_provider_survives_one_provider_failing():
    failing = _StubProvider(error=True)
    working = _StubProvider([_article("http://example.com/1")])

    composite = CompositeNewsProvider([failing, working])
    articles = composite.get_news("NVDA")

    assert [a.url for a in articles] == ["http://example.com/1"]
