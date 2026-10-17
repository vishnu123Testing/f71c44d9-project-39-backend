"""Unit tests for AI forecast recalculation background worker task and execution [FR-010, NFR-002, NFR-004]."""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from src.models.database import Base
from src.models.deal import Deal, DealForecast
from src.models.forecast import ForecastEvaluationLog
from src.services.deals.service import create_deal
from src.services.forecasting.crm_port import CRMDataPort, DealStageHistoryDTO
from src.services.forecasting.forecast_port import (
    ForecastPredictionResult,
    ForecastProviderPort,
)
from src.services.forecasting.service import ForecastDomain
from src.workers.forecast_worker import (
    recalculate_deal_forecast_task,
    recalculate_forecast_task,
    run_forecast_recalculation,
)


@pytest.fixture
def test_db_session() -> Session:
    """Fixture providing an isolated in-memory SQLite database session for unit testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(
        autocommit=False, autoflush=False, bind=engine
    )
    session = testing_session_local()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def sample_deal(test_db_session: Session) -> Deal:
    """Fixture creating a sample deal record in the test database."""
    return create_deal(
        session=test_db_session,
        title="AI Forecast Worker Test Deal",
        deal_value=250000.00,
        milestone_stage="Proposal",
    )


class MockCRMDataPort(CRMDataPort):
    """Mock CRM port implementation for testing."""

    def __init__(self, history: list[DealStageHistoryDTO] | None = None) -> None:
        self.history = history or []

    def get_deal_stage_history(self, deal_id: str) -> list[DealStageHistoryDTO]:
        return self.history

    def get_deal_activities(self, deal_id: str) -> list:
        return []

    def get_stakeholders(self, deal_id: str) -> list:
        return []


class MockForecastProviderPort(ForecastProviderPort):
    """Mock Forecast Provider port implementation for testing."""

    def __init__(self, result: ForecastPredictionResult | None = None) -> None:
        self.result = result

    def predict_forecast(
        self,
        deal_id: str,
        crm_data: Any = None,
        crm_port: CRMDataPort | None = None,
    ) -> ForecastPredictionResult:
        if self.result:
            return self.result
        now = datetime.now(timezone.utc)
        return ForecastPredictionResult(
            deal_id=deal_id,
            estimated_completion_date=now + timedelta(days=45),
            confidence_score_pct=85.0,
            status_code="READY",
            calculated_on=now,
        )


def test_run_forecast_recalculation_success(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Verify run_forecast_recalculation updates forecast DB model and records evaluation log [FR-010, NFR-004]."""
    mock_crm = MockCRMDataPort()
    mock_forecast = MockForecastProviderPort()

    summary = run_forecast_recalculation(
        db=test_db_session,
        deal_id=sample_deal.id,
        crm_port=mock_crm,
        forecast_port=mock_forecast,
        trigger_source="stage_update",
        correlation_id="test_corr_123",
    )

    assert summary["deal_id"] == str(sample_deal.id)
    assert summary["status_code"] == "READY"
    assert summary["confidence_score_pct"] == 85.0
    assert summary["trigger_source"] == "stage_update"
    assert summary["correlation_id"] == "test_corr_123"
    assert summary["estimated_completion_date"] is not None

    # Assert DealForecast persistence
    forecast_model = test_db_session.scalar(
        select(DealForecast).where(DealForecast.deal_id == sample_deal.id)
    )
    assert forecast_model is not None
    assert forecast_model.status_code == "READY"
    assert forecast_model.confidence_score_pct == 85.0

    # Assert ForecastEvaluationLog persistence
    logs = test_db_session.scalars(
        select(ForecastEvaluationLog).where(
            ForecastEvaluationLog.deal_id == sample_deal.id
        )
    ).all()
    assert len(logs) == 1
    assert logs[0].trigger_source == "stage_update"
    assert logs[0].correlation_id == "test_corr_123"


def test_run_forecast_recalculation_sparse_data(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Verify run_forecast_recalculation handles INSUFFICIENT_DATA fallback correctly."""
    now = datetime.now(timezone.utc)
    sparse_result = ForecastPredictionResult(
        deal_id=str(sample_deal.id),
        estimated_completion_date=None,
        confidence_score_pct=None,
        status_code="INSUFFICIENT_DATA",
        calculated_on=now,
        is_sparse=True,
    )
    mock_forecast = MockForecastProviderPort(result=sparse_result)

    summary = run_forecast_recalculation(
        db=test_db_session,
        deal_id=sample_deal.id,
        forecast_port=mock_forecast,
        trigger_source="value_update",
    )

    assert summary["deal_id"] == str(sample_deal.id)
    assert summary["status_code"] == "INSUFFICIENT_DATA"
    assert summary["confidence_score_pct"] is None
    assert summary["estimated_completion_date"] is None


def test_run_forecast_recalculation_deal_not_found(test_db_session: Session) -> None:
    """Verify run_forecast_recalculation handles non-existent deal_id gracefully."""
    random_deal_id = uuid.uuid4()

    summary = run_forecast_recalculation(
        db=test_db_session,
        deal_id=random_deal_id,
        trigger_source="stage_update",
    )

    assert summary["deal_id"] == str(random_deal_id)
    assert summary["status_code"] == "FAILED"
    assert "not found" in summary["error_detail"].lower()


def test_recalculate_deal_forecast_task_execution(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Verify Celery task execution wraps SessionLocal and executes forecast recalculation."""
    with patch("src.workers.forecast_worker.SessionLocal", return_value=test_db_session):
        with patch("src.workers.forecast_worker.ForecastingService") as mock_service_cls:
            mock_service = MagicMock()
            mock_service_cls.return_value = mock_service

            now = datetime.now(timezone.utc)

            mock_service.recalculate_forecast.return_value = ForecastDomain(
                id=uuid.uuid4(),
                deal_id=sample_deal.id,
                estimated_completion_date=now + timedelta(days=30),
                confidence_score_pct=90.0,
                status_code="READY",
                calculated_on=now,
                trigger_source="celery_task",
            )

            result = recalculate_deal_forecast_task(
                deal_id=str(sample_deal.id),
                trigger_source="celery_task",
                correlation_id="corr_celery_789",
            )

            assert result["deal_id"] == str(sample_deal.id)
            assert result["status_code"] == "READY"
            assert result["confidence_score_pct"] == 90.0
            assert result["correlation_id"] == "corr_celery_789"


def test_task_aliases() -> None:
    """Verify task function aliases point to the correct Celery task object."""
    assert recalculate_forecast_task == recalculate_deal_forecast_task
