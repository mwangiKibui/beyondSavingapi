"""Keep what people type into search out of the server logs (ab-307).

uvicorn's access log writes each request line, query string included, so
GET /search?q=<text> would put search text - amounts, payee and person
names - into the logs. This filter swaps /search's query string for
[redacted] and leaves every other path alone.
"""

import logging

ACCESS_LOGGER = "uvicorn.access"
REDACTED_PATHS = ("/search",)


class SearchQueryRedactor(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # uvicorn passes (client, method, full_path, http_version, status).
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path, sep, _query = args[2].partition("?")
            if sep and path in REDACTED_PATHS:
                record.args = args[:2] + (f"{path}?q=[redacted]",) + args[3:]
        return True


def install_search_redaction() -> None:
    logger = logging.getLogger(ACCESS_LOGGER)
    if not any(isinstance(f, SearchQueryRedactor) for f in logger.filters):
        logger.addFilter(SearchQueryRedactor())
