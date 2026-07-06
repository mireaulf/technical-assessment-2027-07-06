import anthropic
from fastapi import HTTPException

from app.analysis import get_ticker_analysis
from app.config import settings
from app.models import ChatRequest, ChatResponse, TickerAnalysis

SYSTEM_PROMPT = """\
You are a financial analyst assistant. You explain stock price movements \
using the structured price and news data provided below. Only use the data \
given to you - if the news data doesn't cover a movement, say so plainly \
instead of guessing. Be concise and cite article titles when you use them.

{context}
"""


def _format_context(analysis: TickerAnalysis) -> str:
    lines = [
        f"Ticker: {analysis.ticker}",
        f"Date range: {analysis.start_date} to {analysis.end_date}",
        f"Data actually ingested for this ticker: {analysis.data_coverage_start} to {analysis.data_coverage_end}",
        f"Movement threshold: >= {analysis.min_move_pct}% single-day change",
        f"Number of flagged movements: {len(analysis.movements)}",
        "",
    ]
    if not analysis.movements:
        lines.append("No movements met the threshold in this date range.")
    for m in analysis.movements:
        lines.append(
            f"- {m.date}: {m.direction.upper()} {m.pct_change:+.2f}% "
            f"(close {m.prev_close} -> {m.close})"
        )
        if m.explanation:
            lines.append(f"    Pre-generated explanation: {m.explanation}")
        if m.articles:
            for a in m.articles:
                published = a.published_at.date() if a.published_at else "unknown date"
                lines.append(f"    * [{published}] {a.title} ({a.source or 'unknown source'})")
        else:
            lines.append("    * No related news articles found for this date.")
    return "\n".join(lines)


def answer_chat(request: ChatRequest) -> ChatResponse:
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=400,
            detail="ANTHROPIC_API_KEY is not set. Add it to your .env file to use the chat endpoint.",
        )

    analysis = get_ticker_analysis(
        request.ticker, request.start_date, request.end_date, request.min_move_pct
    )
    system = SYSTEM_PROMPT.format(context=_format_context(analysis))

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    messages = [{"role": m.role, "content": m.content} for m in request.history]
    messages.append({"role": "user", "content": request.message})

    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=1024,
        system=system,
        messages=messages,
    )
    reply = "".join(block.text for block in response.content if block.type == "text")

    return ChatResponse(
        reply=reply,
        ticker=analysis.ticker,
        movements_considered=len(analysis.movements),
    )
