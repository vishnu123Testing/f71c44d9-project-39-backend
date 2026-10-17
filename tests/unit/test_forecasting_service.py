"""Unit tests for ForecastingService, forecast recalculations, fallback handling, and explicit port DI."""

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from src.connectors.crm_connector import ExternalCRMConnector
from src.connectors.forecast_connector import AIForecastConnector
from src.models.database import Base
from src.models.deal import Deal, DealForecast
from src.models.forecast import ForecastEvaluationLog
from src.services.forecasting.crm_port import (
    CRMActivityDTO,
    CRMDataPort,
    CRMDataResult,
    CRMStakeholderDTO,
    DealStageHistoryDTO,
)
from src.services.forecasting.forecast_port import (
    ForecastPredictionResult,
    ForecastProviderPort,
)
from src.services.forecasting.service import (
    DealNotFoundError,
    ForecastDomain,
    ForecastEvaluationLogDomain,
    ForecastNotFoundError,
    ForecastingService,
    InvalidForecastDataError,
    calculate_forecast,
    get_forecast_by_deal_id,
    get_forecast_evaluation_logs,
    recalculate_forecast,
)


@pytest.fixture
def test_db_session() -> Session:
    """Fixture providing an isolated in-memory SQLite database session."""
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


class MockCRMDataPort(CRMDataPort):
    """Mock CRM port implementation for testing."""

    def __init__(self, history: list[DealStageHistoryDTO] | None = None) -> None:
        self.history = history or []

    def get_deal_stage_history(self, deal_id: str) -> list[DealStageHistoryDTO]:
        return self.history

    def get_deal_activities(self, deal_id: str) -> list[CRMActivityDTO]:
        return []

    def get_stakeholders(self, deal_id: str) -> list[CRMStakeholderDTO]:
        return []


class MockForecastProviderPort(ForecastProviderPort):
    """Mock Forecast Provider port implementation for testing."""

    def __init__(self, result: ForecastPredictionResult | None = None) -> None:
        self.result = result

    def predict_forecast(
        self,
        deal_id: str,
        crm_data: CRMDataResult | None = None,
        crm_port: CRMDataPort | None = None,
    ) -> ForecastPredictionResult:
        if self.result:
            return self.result
        now = datetime.now(timezone.utc)
        return ForecastPredictionResult(
            deal_id=deal_id,
            estimated_completion_date=now + timedelta(days=30),
            confidence_score_pct=88.0,
            status_code="READY",
            calculated_on=now,
        )


def test_forecasting_service_initialization_and_di() -> None:
    """Test initializing ForecastingService with explicit port dependency injection (NFR-005)."""
    mock_crm = MockCRMDataPort()
    mock_forecast = MockForecastProviderPort()

    service = ForecastingService(crm_port=mock_crm, forecast_port=mock_forecast)

    assert service.crm_port is mock_crm
    assert service.forecast_port is mock_forecast


def test_calculate_forecast_invalid_deal_id() -> None:
    """Test that empty or whitespace deal_id raises InvalidForecastDataError."""
    service = ForecastingService()

    with pytest.raises(InvalidForecastDataError, match="deal_id must be a non-empty string or UUID"):
        service.calculate_forecast("")

    with pytest.raises(InvalidForecastDataError, match="deal_id must be a non-empty string or UUID"):
        service.calculate_forecast("   ")


def test_calculate_forecast_without_db_session() -> None:
    """Test calculating forecast without a database session returns a valid ForecastDomain."""
    mock_crm = MockCRMDataPort()
    now = datetime.now(timezone.utc)
    est_date = now + timedelta(days=45)
    mock_forecast = MockForecastProviderPort(
        result=ForecastPredictionResult(
            deal_id="deal_test_1",
            estimated_completion_date=est_date,
            confidence_score_pct=92.5,
            status_code="READY",
            calculated_on=now,
        )
    )

    service = ForecastingService(crm_port=mock_crm, forecast_port=mock_forecast)
    domain = service.calculate_forecast("deal_test_1")

    assert isinstance(domain, ForecastDomain)
    assert domain.status_code == "READY"
    assert domain.confidence_score_pct == 92.5
    assert domain.estimated_completion_date == est_date
    assert domain.id is None


def test_calculate_forecast_deal_not_found_in_db(test_db_session: Session) -> None:
    """Test that calculating forecast with db_session when deal is absent raises DealNotFoundError."""
    service = ForecastingService()
    missing_id = uuid.uuid4()

    with pytest.raises(DealNotFoundError, match="found in database"):
        service.calculate_forecast(missing_id, db_session=test_db_session)


def test_calculate_forecast_successful_persistence(test_db_session: Session) -> None:
    """Test calculating forecast with db_session persists DealForecast and ForecastEvaluationLog."""
    deal = Deal(
        title="Enterprise Software Expansion",
        deal_value=120000.0,
        milestone_stage="Proposal",
        win_probability=0.6,
    )
    test_db_session.add(deal)
    test_db_session.commit()

    service = ForecastingService(
        crm_port=ExternalCRMConnector(),
        forecast_port=AIForecastConnector(),
    )

    domain = service.calculate_forecast(deal.id, db_session=test_db_session, trigger_source="stage_change")

    assert domain.id is not None
    assert domain.deal_id == deal.id
    assert domain.status_code == "READY"
    assert domain.confidence_score_pct is not None
    assert domain.estimated_completion_date is not None

    # Verify DealForecast in DB
    db_forecast = test_db_session.scalar(select(DealForecast).where(DealForecast.deal_id == deal.id))
    assert db_forecast is not None
    assert db_forecast.status_code == "READY"
    assert db_forecast.confidence_score_pct == domain.confidence_score_pct

    # Verify ForecastEvaluationLog in DB
    db_logs = test_db_session.scalars(
        select(ForecastEvaluationLog).where(ForecastEvaluationLog.deal_id == deal.id)
    ).all()
    assert len(db_logs) == 1
    assert db_logs[0].trigger_source == "stage_change"
    assert db_logs[0].status_code == "READY"


def test_calculate_forecast_sparse_data_fallback(test_db_session: Session) -> None:
    """Test sparse CRM history fallback handling returning INSUFFICIENT_DATA status (FR-008, FR-009)."""
    deal = Deal(
        title="Newly Inbound Prospect",
        deal_value=20000.0,
        milestone_stage="Qualification",
        win_probability=0.2,
    )
    test_db_session.add(deal)
    test_db_session.commit()

    # Connector configured with simulate_sparse_data=True
    sparse_forecast_port = AIForecastConnector(simulate_sparse_data=True)
    service = ForecastingService(forecast_port=sparse_forecast_port)

    domain = service.calculate_forecast(deal.id, db_session=test_db_session, trigger_source="deal_created")

    assert domain.status_code == "INSUFFICIENT_DATA"
    assert domain.estimated_completion_date is None
    assert domain.confidence_score_pct is None
    assert domain.is_sparse is True
    assert "Sparse CRM" in (domain.error_detail or "")

    # Verify DB persistence
    db_forecast = test_db_session.scalar(select(DealForecast).where(DealForecast.deal_id == deal.id))
    assert db_forecast is not None
    assert db_forecast.status_code == "INSUFFICIENT_DATA"
    assert db_forecast.estimated_completion_date is None


def test_calculate_forecast_provider_failure_fallback(test_db_session: Session) -> None:
    """Test upstream forecast provider failure fallback returning FAILED status gracefully."""
    deal = Deal(
        title="Faulty Upstream Integration Deal",
        deal_value=50000.0,
        milestone_stage="Needs Analysis",
        win_probability=0.35,
    )
    test_db_session.add(deal)
    test_db_session.commit()

    failing_port = AIForecastConnector(simulate_failure=True)
    service = ForecastingService(forecast_port=failing_port)

    domain = service.calculate_forecast(deal.id, db_session=test_db_session)

    assert domain.status_code == "FAILED"
    assert domain.estimated_completion_date is None
    assert domain.confidence_score_pct is None
    assert "Simulated upstream" in (domain.error_detail or "")


def test_recalculate_forecast_updates_existing_record(test_db_session: Session) -> None:
    """Test recalculating forecast updates existing DealForecast and appends evaluation log (FR-010)."""
    deal = Deal(
        title="Multi-Stage Strategic Account",
        deal_value=300000.0,
        milestone_stage="Needs Analysis",
        win_probability=0.35,
    )
    test_db_session.add(deal)
    test_db_session.commit()

    service = ForecastingService(
        crm_port=ExternalCRMConnector(),
        forecast_port=AIForecastConnector(),
    )

    # Initial calculation
    d1 = service.calculate_forecast(deal.id, db_session=test_db_session, trigger_source="deal_created")
    assert d1.status_code == "READY"

    # Deal stage changes
    deal.milestone_stage = "Negotiation"
    deal.win_probability = 0.8
    test_db_session.commit()

    # Recalculate
    d2 = service.recalculate_forecast(deal.id, db_session=test_db_session, trigger_source="milestone_stage_change")

    assert d2.deal_id == deal.id
    assert d2.status_code == "READY"

    # Confirm only 1 DealForecast record exists
    forecasts = test_db_session.scalars(select(DealForecast).where(DealForecast.deal_id == deal.id)).all()
    assert len(forecasts) == 1

    # Confirm 2 ForecastEvaluationLog entries exist
    logs = test_db_session.scalars(
        select(ForecastEvaluationLog)
        .where(ForecastEvaluationLog.deal_id == deal.id)
        .order_by(ForecastEvaluationLog.recalculated_on.asc())
    ).all()
    assert len(logs) == 2
    assert logs[0].trigger_source == "deal_created"
    assert logs[1].trigger_source == "milestone_stage_change"


def test_get_forecast_by_deal_id(test_db_session: Session) -> None:
    """Test retrieving existing forecast from DB or auto-calculating if missing."""
    deal = Deal(
        title="Query Test Deal",
        deal_value=85000.0,
        milestone_stage="Proposal",
        win_probability=0.6,
    )
    test_db_session.add(deal)
    test_db_session.commit()

    service = ForecastingService()

    # When missing and auto_calculate=False -> raises ForecastNotFoundError
    with pytest.raises(ForecastNotFoundError, match="not found"):
        service.get_forecast_by_deal_id(deal.id, db_session=test_db_session, auto_calculate=False)

    # When missing and auto_calculate=True -> calculates and returns
    domain1 = service.get_forecast_by_deal_id(deal.id, db_session=test_db_session, auto_calculate=True)
    assert domain1.deal_id == deal.id
    assert domain1.status_code == "READY"

    # Subsequent call fetches existing record directly
    domain2 = service.get_forecast_by_deal_id(deal.id, db_session=test_db_session, auto_calculate=False)
    assert domain2.id == domain1.id
    assert domain2.status_code == "READY"


def test_get_forecast_evaluation_logs(test_db_session: Session) -> None:
    """Test querying chronological forecast evaluation logs for a deal."""
    deal = Deal(
        title="Evaluation Log History Deal",
        deal_value=90000.0,
        milestone_stage="Qualification",
        win_probability=0.2,
    )
    test_db_session.add(deal)
    test_db_session.commit()

    service = ForecastingService()
    service.calculate_forecast(deal.id, db_session=test_db_session, trigger_source="source_1")
    service.recalculate_forecast(deal.id, db_session=test_db_session, trigger_source="source_2")

    logs = service.get_forecast_evaluation_logs(deal.id, db_session=test_db_session)
    assert len(logs) == 2
    assert all(isinstance(log, ForecastEvaluationLogDomain) for log in logs)
    assert logs[0].trigger_source == "source_2"
    assert logs[1].trigger_source == "source_1"


def test_top_level_functional_entrypoints(test_db_session: Session) -> None:
    """Test top-level functional helper entrypoints."""
    deal = Deal(
        title="Functional API Test Deal",
        deal_value=150000.0,
        milestone_stage="Needs Analysis",
        win_probability=0.35,
    )
    test_db_session.add(deal)
    test_db_session.commit()

    # calculate_forecast
    f1 = calculate_forecast(deal.id, db_session=test_db_session, trigger_source="func_calc")
    assert f1.status_code == "READY"

    # recalculate_forecast
    f2 = recalculate_forecast(deal.id, db_session=test_db_session, trigger_source="func_recalc")
    assert f2.status_code == "READY"

    # get_forecast_by_deal_id
    f3 = get_forecast_by_deal_id(deal.id, db_session=test_db_session)
    assert f3.status_code == "READY"

    # get_forecast_evaluation_logs
    logs = get_forecast_evaluation_logs(deal.id, db_session=test_db_session)
    assert len(logs) == 2
