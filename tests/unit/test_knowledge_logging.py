"""Unit tests for structured JSON logging, correlation ID context propagation, and domain exception handling in knowledge API [NFR-4]."""

from unittest.mock import MagicMock, patch
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from src.api.deps import CurrentUser, get_current_user
from src.api.knowledge import router as knowledge_router
from src.lib.logging import CORRELATION_ID_HEADER, CorrelationIdMiddleware
from src.models.database import get_db
from src.services.knowledge.article_service import (
    ArticleNotFoundError,
    InvalidArticleDataError,
)
from src.services.knowledge.playbook_service import (
    InvalidStepReferenceError,
    PlaybookNotFoundError,
)


@pytest.fixture
def mock_db() -> MagicMock:
    """Mock SQLAlchemy database session."""
    return MagicMock()


@pytest.fixture
def test_app(mock_db: MagicMock) -> FastAPI:
    """FastAPI test app configured with CorrelationIdMiddleware and knowledge router."""
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    mock_user = CurrentUser(
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        email="test@example.com",
    )

    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: mock_user

    app.include_router(knowledge_router, prefix="/api/v1")
    return app


def test_knowledge_api_correlation_id_propagation(test_app: FastAPI) -> None:
    """Verify custom correlation ID header is propagated in knowledge API response."""
    client = TestClient(test_app)
    custom_corr_id = "test-corr-id-knowledge-123"

    with patch("src.api.knowledge.search_articles") as mock_search:
        mock_result = MagicMock()
        mock_result.items = []
        mock_result.total = 0
        mock_result.page = 1
        mock_result.limit = 20
        mock_result.total_pages = 0
        mock_search.return_value = mock_result

        response = client.get(
            "/api/v1/knowledge/articles",
            headers={CORRELATION_ID_HEADER: custom_corr_id},
        )

        assert response.status_code == 200
        assert response.headers[CORRELATION_ID_HEADER] == custom_corr_id


def test_knowledge_api_domain_exception_handling_404(test_app: FastAPI) -> None:
    """Verify ArticleNotFoundError is caught and converted to HTTP 404 with warning log."""
    client = TestClient(test_app)
    fake_id = str(uuid.uuid4())

    with patch("src.api.knowledge.get_article_by_id") as mock_get:
        mock_get.side_effect = ArticleNotFoundError(f"Knowledge article with ID '{fake_id}' was not found.")

        with patch("src.api.knowledge.logger") as mock_logger:
            response = client.get(f"/api/v1/knowledge/articles/{fake_id}")

            assert response.status_code == 404
            assert "not found" in response.json()["detail"].lower()
            assert mock_logger.warning.called


def test_knowledge_api_domain_exception_handling_400(test_app: FastAPI) -> None:
    """Verify InvalidArticleDataError is caught and converted to HTTP 400 with warning log."""
    client = TestClient(test_app)

    with patch("src.api.knowledge.search_articles") as mock_search:
        mock_search.side_effect = InvalidArticleDataError("Invalid content type 'invalid'")

        with patch("src.api.knowledge.logger") as mock_logger:
            response = client.get("/api/v1/knowledge/articles?content_type=invalid")

            assert response.status_code == 400
            assert "Invalid content type" in response.json()["detail"]
            assert mock_logger.warning.called


def test_knowledge_api_unexpected_exception_handling_500(test_app: FastAPI) -> None:
    """Verify unexpected Exception is caught, logged with error level and exc_info, and converted to HTTP 500."""
    client = TestClient(test_app)
    fake_id = str(uuid.uuid4())

    with patch("src.api.knowledge.get_article_by_id") as mock_get:
        mock_get.side_effect = RuntimeError("Database connection lost")

        with patch("src.api.knowledge.logger") as mock_logger:
            response = client.get(f"/api/v1/knowledge/articles/{fake_id}")

            assert response.status_code == 500
            assert "unexpected error" in response.json()["detail"].lower()
            assert mock_logger.error.called
