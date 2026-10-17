import contextvars
from datetime import datetime, timezone
import json
import logging
import sys
from typing import Any
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

CORRELATION_ID_HEADER = "X-Correlation-ID"

_correlation_id_ctx_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)

SENSITIVE_KEYS = {
    "password",
    "pass",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "authorization",
    "api_key",
    "credentials",
    "private_key",
}

RESERVED_RECORD_ATTRS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "message",
    "module",
    "msecs",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
    "correlation_id",
}


def get_correlation_id() -> str | None:
    """Retrieve the current request correlation ID from context."""
    return _correlation_id_ctx_var.get()


def set_correlation_id(correlation_id: str | None) -> contextvars.Token[str | None]:
    """Set the correlation ID in context for the current request scope."""
    return _correlation_id_ctx_var.set(correlation_id)


def reset_correlation_id(token: contextvars.Token[str | None]) -> None:
    """Reset the correlation ID in context using the provided token."""
    _correlation_id_ctx_var.reset(token)


def _mask_value(key: str, value: Any) -> Any:
    """Mask sensitive string values if key matches sensitive key patterns."""
    if isinstance(key, str) and any(
        sensitive_key in key.lower() for sensitive_key in SENSITIVE_KEYS
    ):
        return "***"
    if isinstance(value, dict):
        return {k: _mask_value(str(k), v) for k, v in value.items()}
    return value


class JSONFormatter(logging.Formatter):
    """Structured JSON log formatter for machine-readable logging to standard output."""

    def format(self, record: logging.LogRecord) -> str:
        """Format a LogRecord into a structured JSON string."""
        record_corr_id = getattr(record, "correlation_id", None)
        corr_id = record_corr_id or get_correlation_id()

        log_data: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": corr_id,
        }

        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        # Include extra custom fields attached to record
        for key, value in record.__dict__.items():
            if key not in RESERVED_RECORD_ATTRS and not key.startswith("_"):
                log_data[key] = _mask_value(key, value)

        return json.dumps(log_data, default=str)


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """ASGI middleware injecting and propagating correlation IDs for request tracing."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Process incoming request, set correlation ID context, and propagate header."""
        header_correlation_id = request.headers.get(CORRELATION_ID_HEADER)
        correlation_id = (
            header_correlation_id.strip()
            if header_correlation_id and header_correlation_id.strip()
            else str(uuid.uuid4())
        )

        token = set_correlation_id(correlation_id)
        try:
            response = await call_next(request)
            response.headers[CORRELATION_ID_HEADER] = correlation_id
            return response
        finally:
            reset_correlation_id(token)


def setup_logging(log_level: str = "INFO") -> None:
    """Configure root logger with structured JSON handler pointing to standard output."""
    level: int = getattr(logging, log_level.upper(), logging.INFO)
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove existing handlers to avoid duplicate logs
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    handler.setLevel(level)
    root_logger.addHandler(handler)
