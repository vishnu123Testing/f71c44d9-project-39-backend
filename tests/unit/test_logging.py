import json
import logging
import sys
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from src.lib.logging import (
    CORRELATION_ID_HEADER,
    CorrelationIdMiddleware,
    JSONFormatter,
    get_correlation_id,
    reset_correlation_id,
    set_correlation_id,
    setup_logging,
)


def test_correlation_id_context_management() -> None:
    """Test setting, getting, and resetting context correlation ID."""
    assert get_correlation_id() is None

    test_id = "test-corr-id-123"
    token = set_correlation_id(test_id)
    assert get_correlation_id() == test_id

    reset_correlation_id(token)
    assert get_correlation_id() is None


def test_json_formatter_basic_fields() -> None:
    """Test that JSONFormatter outputs required standard log fields."""
    formatter = JSONFormatter()
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname="test_path.py",
        lineno=10,
        msg="Test log message",
        args=(),
        exc_info=None,
    )

    formatted_str = formatter.format(record)
    data = json.loads(formatted_str)

    assert data["level"] == "INFO"
    assert data["logger"] == "test_logger"
    assert data["message"] == "Test log message"
    assert data["correlation_id"] is None
    assert "timestamp" in data


def test_json_formatter_with_context_correlation_id() -> None:
    """Test that JSONFormatter includes correlation ID from context."""
    formatter = JSONFormatter()
    corr_id = "corr-456"
    token = set_correlation_id(corr_id)

    try:
        record = logging.LogRecord(
            name="test_logger",
            level=logging.WARNING,
            pathname="test_path.py",
            lineno=20,
            msg="Warning message",
            args=(),
            exc_info=None,
        )
        formatted_str = formatter.format(record)
        data = json.loads(formatted_str)

        assert data["correlation_id"] == corr_id
    finally:
        reset_correlation_id(token)


def test_json_formatter_extra_fields_and_sensitive_data_masking() -> None:
    """Test extra fields inclusion and sensitive field masking in JSON logs."""
    formatter = JSONFormatter()
    record = logging.LogRecord(
        name="auth_logger",
        level=logging.INFO,
        pathname="auth.py",
        lineno=30,
        msg="User login attempt",
        args=(),
        exc_info=None,
    )

    record.__dict__["user_id"] = "user-789"
    record.__dict__["action"] = "login"
    record.__dict__["password"] = "supersecret123"
    record.__dict__["access_token"] = "eyJhbGciOi..."

    formatted_str = formatter.format(record)
    data = json.loads(formatted_str)

    assert data["user_id"] == "user-789"
    assert data["action"] == "login"
    assert data["password"] == "***"
    assert data["access_token"] == "***"


def test_json_formatter_exception_logging() -> None:
    """Test exception formatting in JSON log output."""
    formatter = JSONFormatter()

    try:
        raise ValueError("Something went wrong")
    except ValueError:
        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="error_logger",
        level=logging.ERROR,
        pathname="err.py",
        lineno=40,
        msg="Error occurred",
        args=(),
        exc_info=exc_info,
    )

    formatted_str = formatter.format(record)
    data = json.loads(formatted_str)

    assert data["level"] == "ERROR"
    assert "exception" in data
    assert "ValueError: Something went wrong" in data["exception"]


def test_correlation_id_middleware_generated_header() -> None:
    """Test middleware generates correlation ID when missing from request headers."""
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    captured_id: list[str | None] = []

    @app.get("/test")
    def test_endpoint() -> dict[str, str]:
        captured_id.append(get_correlation_id())
        return {"status": "ok"}

    client = TestClient(app)
    response = client.get("/test")

    assert response.status_code == 200
    assert CORRELATION_ID_HEADER in response.headers

    header_corr_id = response.headers[CORRELATION_ID_HEADER]
    # Validate it's a valid UUID
    uuid.UUID(header_corr_id)
    assert captured_id[0] == header_corr_id

    # Ensure context reset after request
    assert get_correlation_id() is None


def test_correlation_id_middleware_propagates_existing_header() -> None:
    """Test middleware propagates existing correlation ID from request headers."""
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    captured_id: list[str | None] = []

    @app.get("/test")
    def test_endpoint() -> dict[str, str]:
        captured_id.append(get_correlation_id())
        return {"status": "ok"}

    client = TestClient(app)
    custom_corr_id = "custom-correlation-id-999"
    response = client.get("/test", headers={CORRELATION_ID_HEADER: custom_corr_id})

    assert response.status_code == 200
    assert response.headers[CORRELATION_ID_HEADER] == custom_corr_id
    assert captured_id[0] == custom_corr_id
    assert get_correlation_id() is None


def test_setup_logging_configuration() -> None:
    """Test setup_logging configures root logger with JSONFormatter and stdout stream."""
    try:
        setup_logging("DEBUG")
        root_logger = logging.getLogger()

        assert root_logger.level == logging.DEBUG
        assert len(root_logger.handlers) == 1

        handler = root_logger.handlers[0]
        assert isinstance(handler, logging.StreamHandler)
        assert handler.stream == sys.stdout
        assert isinstance(handler.formatter, JSONFormatter)
    finally:
        root_logger = logging.getLogger()
        for h in root_logger.handlers[:]:
            root_logger.removeHandler(h)
        root_logger.setLevel(logging.NOTSET)
