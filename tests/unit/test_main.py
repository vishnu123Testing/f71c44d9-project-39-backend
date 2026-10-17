"""Unit tests for FastAPI main application entrypoint, middleware, and lifespan setup."""

from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from src.api.main import app, lifespan
from src.lib.config import settings
from src.lib.logging import CORRELATION_ID_HEADER, CorrelationIdMiddleware
from src.models.database import Base, engine


def test_app_metadata_and_configuration() -> None:
    """Test that FastAPI app instance is created with expected configuration."""
    assert app.title == settings.APP_NAME
    assert app.debug == settings.DEBUG
    assert app.openapi_url == f"{settings.API_V1_STR}/openapi.json"
    assert app.docs_url == f"{settings.API_V1_STR}/docs"
    assert app.redoc_url == f"{settings.API_V1_STR}/redoc"


def test_middleware_registration() -> None:
    """Test that CorrelationIdMiddleware and CORSMiddleware are registered on app."""
    middleware_classes = [m.cls for m in app.user_middleware]
    assert CorrelationIdMiddleware in middleware_classes


def test_health_check_endpoints() -> None:
    """Test health check endpoints return 200 OK with expected JSON structure."""
    with TestClient(app) as client:
        response_root = client.get("/health")
        assert response_root.status_code == 200
        assert response_root.json() == {
            "status": "ok",
            "environment": settings.ENVIRONMENT,
        }

        response_v1 = client.get(f"{settings.API_V1_STR}/health")
        assert response_v1.status_code == 200
        assert response_v1.json() == {
            "status": "ok",
            "environment": settings.ENVIRONMENT,
        }


def test_correlation_id_middleware_integration() -> None:
    """Test that requests propagate and return X-Correlation-ID header."""
    with TestClient(app) as client:
        custom_corr_id = "test-correlation-id-999"
        response = client.get("/health", headers={CORRELATION_ID_HEADER: custom_corr_id})

        assert response.status_code == 200
        assert response.headers.get(CORRELATION_ID_HEADER) == custom_corr_id


def test_dashboard_router_registered() -> None:
    """Test that dashboard router routes are registered on the FastAPI application."""
    route_paths = [getattr(route, "path", "") for route in app.routes]
    assert f"{settings.API_V1_STR}/dashboard/summary" in route_paths
    assert f"{settings.API_V1_STR}/dashboard/domains/{{domain_id}}" in route_paths


@pytest.mark.asyncio
async def test_lifespan_hook_execution() -> None:
    """Test that lifespan context manager initializes logging and database tables."""
    with (
        patch("src.api.main.setup_logging") as mock_setup_logging,
        patch.object(Base.metadata, "create_all") as mock_create_all,
    ):
        async with lifespan(app):
            mock_setup_logging.assert_called_once_with(settings.LOG_LEVEL)
            mock_create_all.assert_called_once_with(bind=engine)
