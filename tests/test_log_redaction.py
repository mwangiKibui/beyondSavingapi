import logging

from app.core.log_redaction import SearchQueryRedactor, install_search_redaction


def _access_record(path: str, status: int = 200) -> logging.LogRecord:
    # The shape uvicorn's access logger uses: client, method, full path, http version, status.
    return logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=1,
        msg='%s - "%s %s HTTP/%s" %d', args=("172.18.0.1:5000", "GET", path, "1.1", status), exc_info=None,
    )


def test_search_query_is_redacted():
    record = _access_record("/search?q=GRACE%202600&type=transactions", 200)
    assert SearchQueryRedactor().filter(record) is True
    line = record.getMessage()
    assert "GRACE" not in line and "2600" not in line
    assert "/search?q=[redacted]" in line
    assert "200" in line


def test_search_without_query_string_is_left_alone():
    record = _access_record("/search")
    SearchQueryRedactor().filter(record)
    assert '"GET /search HTTP/1.1" 200' in record.getMessage()


def test_other_paths_are_unchanged():
    record = _access_record("/transactions?search=x")
    SearchQueryRedactor().filter(record)
    assert "/transactions?search=x" in record.getMessage()


def test_paths_that_only_start_with_search_are_unchanged():
    record = _access_record("/searches?q=x")
    SearchQueryRedactor().filter(record)
    assert "/searches?q=x" in record.getMessage()


def test_records_with_other_shapes_pass_through():
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "plain message", None, None)
    assert SearchQueryRedactor().filter(record) is True
    assert record.getMessage() == "plain message"


def test_install_attaches_once_to_uvicorn_access():
    logger = logging.getLogger("uvicorn.access")
    before = [f for f in logger.filters if isinstance(f, SearchQueryRedactor)]
    for f in before:
        logger.removeFilter(f)
    install_search_redaction()
    install_search_redaction()
    assert len([f for f in logger.filters if isinstance(f, SearchQueryRedactor)]) == 1


def test_app_import_installs_the_filter():
    import app.main  # noqa: F401

    logger = logging.getLogger("uvicorn.access")
    assert any(isinstance(f, SearchQueryRedactor) for f in logger.filters)
