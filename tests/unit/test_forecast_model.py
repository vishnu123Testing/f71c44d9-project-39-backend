from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.database import Base
from src.models.deal import Deal
from src.models.forecast import ForecastEvaluationLog, ForecastRecalculationLog


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


def test_forecast_evaluation_log_tablename() -> None:
    """Test table name and model class alias for ForecastEvaluationLog."""
    assert ForecastEvaluationLog.__tablename__ == "forecast_evaluation_logs"
    assert ForecastRecalculationLog is ForecastEvaluationLog


def test_forecast_evaluation_log_creation_and_defaults(test_db_session: Session) -> None:
    """Test creating a ForecastEvaluationLog linked to a Deal with default values."""
    deal = Deal(
        title="Predictive Pipeline Expansion",
        deal_value=250000.00,
        milestone_stage="Negotiation",
        win_probability=0.8,
    )
    test_db_session.add(deal)
    test_db_session.commit()

    est_date = datetime(2026, 11, 30, 0, 0, 0, tzinfo=timezone.utc)
    corr_id = f"corr_fc_{uuid.uuid4()}"

    log_entry = ForecastEvaluationLog(
        deal_id=deal.id,
        estimated_completion_date=est_date,
        confidence_score_pct=85.5,
        status_code="READY",
        trigger_source="stage_change",
        correlation_id=corr_id,
    )
    test_db_session.add(log_entry)
    test_db_session.commit()
    test_db_session.refresh(log_entry)

    assert isinstance(log_entry.id, uuid.UUID)
    assert log_entry.deal_id == deal.id
    assert log_entry.estimated_completion_date == est_date
    assert log_entry.confidence_score_pct == 85.5
    assert log_entry.status_code == "READY"
    assert log_entry.trigger_source == "stage_change"
    assert log_entry.correlation_id == corr_id
    assert log_entry.error_detail is None
    assert isinstance(log_entry.recalculated_on, datetime)
    assert log_entry.deal.title == "Predictive Pipeline Expansion"
    assert repr(log_entry).startswith("<ForecastEvaluationLog(")


def test_forecast_evaluation_log_insufficient_data(test_db_session: Session) -> None:
    """Test creating a ForecastEvaluationLog entry for an INSUFFICIENT_DATA fallback event."""
    deal = Deal(
        title="New Lead without CRM history",
        deal_value=15000.00,
        milestone_stage="Prospecting",
        win_probability=0.1,
    )
    test_db_session.add(deal)
    test_db_session.commit()

    log_entry = ForecastEvaluationLog(
        deal_id=deal.id,
        estimated_completion_date=None,
        confidence_score_pct=None,
        status_code="INSUFFICIENT_DATA",
        trigger_source="deal_created",
        error_detail="Sparse CRM historical stage transitions",
    )
    test_db_session.add(log_entry)
    test_db_session.commit()
    test_db_session.refresh(log_entry)

    assert log_entry.estimated_completion_date is None
    assert log_entry.confidence_score_pct is None
    assert log_entry.status_code == "INSUFFICIENT_DATA"
    assert log_entry.error_detail == "Sparse CRM historical stage transitions"
