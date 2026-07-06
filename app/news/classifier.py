import json
import logging
from typing import Optional

import anthropic
import yfinance as yf

from app.models import TickerClassification

logger = logging.getLogger(__name__)

MAX_COMPETITORS = 3
# Cap how many existing industry labels go into the prompt so it stays
# bounded as more tickers get classified over time.
MAX_EXISTING_INDUSTRIES_IN_PROMPT = 50


def classify_ticker(
    ticker: str, api_key: str, model: str, existing_industries: Optional[list[str]] = None
) -> Optional[TickerClassification]:
    """Best-effort LLM lookup of a ticker's industry + top competitors.

    Drives industry moves (competitor/industry news) without relying on a
    paid peers/fundamentals API - yfinance doesn't reliably expose one.
    Caller is expected to cache the result (see app/ingestion.py); this
    hits the Anthropic API every time it's called. Never raises - a failed
    classification just means industry moves news is skipped for this ticker.

    `existing_industries` (already-classified labels from other tickers,
    see app/repository.py's list_classified_industries) is passed back to
    Claude so it can reuse one instead of independently coining a new but
    equivalent label for the same industry (e.g. "Semiconductors" vs.
    "Semiconductor Manufacturing") - this keeps GET /api/tickers?industry=
    and GET /api/industries actually useful for grouping related tickers,
    rather than every ticker getting its own one-off wording.
    """
    if not api_key:
        return None

    try:
        company_name = yf.Ticker(ticker).info.get("longName")
    except Exception:
        company_name = None

    reuse_hint = (
        f" These industry labels are already in use for other tickers - reuse one of them if it genuinely "
        f"fits this ticker, rather than coining a new but equivalent label: "
        f"{existing_industries[:MAX_EXISTING_INDUSTRIES_IN_PROMPT]}."
        if existing_industries
        else ""
    )
    prompt = (
        f"Stock ticker: {ticker}"
        + (f" ({company_name})" if company_name else "")
        + ". Respond with ONLY compact JSON, no prose, no markdown fences: "
        '{"industry": "<2-4 word industry/sector>", '
        f'"competitors": ["<up to {MAX_COMPETITORS} direct publicly traded competitor company names>"]}}'
        + reuse_hint
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        data = json.loads(text)
        return TickerClassification(
            industry=data["industry"],
            competitors=list(data.get("competitors", []))[:MAX_COMPETITORS],
        )
    except Exception:
        logger.exception("Ticker classification failed for %s", ticker)
        return None
