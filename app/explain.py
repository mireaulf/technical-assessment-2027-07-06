import json
import logging
from datetime import date

import anthropic

from app.config import settings
from app.models import Movement

logger = logging.getLogger(__name__)

PROMPT_TEMPLATE = """\
You are a financial analyst assistant. For each stock movement listed below, \
write a concise 1-2 sentence explanation of why the stock likely moved, based \
only on the news articles given for that date. If none of the articles \
plausibly explain the move, say so plainly instead of guessing.

Respond with ONLY a JSON array, no other text, in this exact shape:
[{{"date": "YYYY-MM-DD", "explanation": "..."}}, ...]
One entry per movement listed below, in the same order.

{movements_block}
"""


def _format_movements_block(ticker: str, movements: list[Movement]) -> str:
    lines = [f"Ticker: {ticker}", ""]
    for m in movements:
        lines.append(
            f"Date: {m.date} | {m.direction.upper()} {m.pct_change:+.2f}% "
            f"(close {m.prev_close} -> {m.close})"
        )
        for a in m.articles:
            published = a.published_at.date() if a.published_at else "unknown date"
            summary = f": {a.summary}" if a.summary else ""
            lines.append(f"  - [{published}] {a.title} ({a.source or 'unknown source'}){summary}")
        lines.append("")
    return "\n".join(lines)


def _parse_json_array(text: str) -> list[dict]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[len("json") :] if text.lower().startswith("json") else text
    return json.loads(text)


def generate_explanations(ticker: str, movements: list[Movement]) -> dict[date, str]:
    """One batched Claude call -> {movement_date: explanation}.

    Only called from ingestion (app/ingestion.py) for movements that already
    have nearby articles attached and no stored explanation yet - never from
    the read path. The API itself never calls Claude to answer "why did this
    move", only to answer free-form chat questions, so it keeps working even
    if Claude is down. Returns {} (a no-op, not an error) if there's nothing
    to explain or no ANTHROPIC_API_KEY configured - ingestion should keep
    persisting price/news data either way.
    """
    if not movements or not settings.anthropic_api_key:
        return {}

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    prompt = PROMPT_TEMPLATE.format(movements_block=_format_movements_block(ticker, movements))

    try:
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        parsed = _parse_json_array(text)
        return {
            date.fromisoformat(item["date"]): item["explanation"]
            for item in parsed
            if item.get("date") and item.get("explanation")
        }
    except Exception:
        logger.exception("Explanation generation failed for %s (prices/news were still ingested)", ticker)
        return {}
