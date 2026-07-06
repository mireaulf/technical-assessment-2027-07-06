import logging
import time

from app.config import settings
from app.db import init_db
from app.ingestion import ingest_tracked_tickers, log_active_news_providers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("scheduler")


def main() -> None:
    # create_all is idempotent, so it's safe to call here too - the worker
    # can then start before the API ever has, e.g. on a brand-new database.
    init_db()
    log_active_news_providers()

    interval = settings.ingestion_interval_seconds
    logger.info("Ingestion scheduler starting, refreshing tracked tickers every %ss", interval)
    while True:
        results = ingest_tracked_tickers()
        if results:
            logger.info("Refreshed %d ticker(s): %s", len(results), [r["ticker"] for r in results])
        else:
            logger.info("No tracked tickers yet - ingest one via POST /api/ingest/{ticker}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
