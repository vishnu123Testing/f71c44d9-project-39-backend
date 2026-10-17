from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from src.models.dashboard import MetricSnapshot
from src.models.database import Base


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


def test_metric_snapshot_tablename() -> None:
    """Test that MetricSnapshot model has the expected table name."""
    assert MetricSnapshot.__tablename__ == "metric_snapshots"


def test_metric_snapshot_creation_and_defaults(test_db_session: Session) -> None:
    """Test creating a MetricSnapshot record with default recorded_at value."""
    snapshot = MetricSnapshot(
        domain_name="lead_gen",
        metric_key="conversion_rate",
        current_value=12.5,
        baseline_value=10.0,
        status_code="VALID",
    )
    test_db_session.add(snapshot)
    test_db_session.commit()
    test_db_session.refresh(snapshot)

    assert isinstance(snapshot.id, int)
    assert snapshot.domain_name == "lead_gen"
    assert snapshot.metric_key == "conversion_rate"
    assert snapshot.current_value == 12.5
    assert snapshot.baseline_value == 10.0
    assert snapshot.status_code == "VALID"
    assert snapshot.error_detail is None
    assert isinstance(snapshot.recorded_at, datetime)


def test_metric_snapshot_unavailable_state(test_db_session: Session) -> None:
    """Test creating a MetricSnapshot record representing an UNAVAILABLE state with error_detail."""
    recorded_time = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)
    snapshot = MetricSnapshot(
        domain_name="support",
        metric_key="open_tickets",
        current_value=None,
        baseline_value=25.0,
        status_code="UNAVAILABLE",
        error_detail="Downstream support API connection timeout",
        recorded_at=recorded_time,
    )
    test_db_session.add(snapshot)
    test_db_session.commit()
    test_db_session.refresh(snapshot)

    assert snapshot.domain_name == "support"
    assert snapshot.metric_key == "open_tickets"
    assert snapshot.current_value is None
    assert snapshot.baseline_value == 25.0
    assert snapshot.status_code == "UNAVAILABLE"
    assert snapshot.error_detail == "Downstream support API connection timeout"
    assert snapshot.recorded_at == recorded_time


def test_metric_snapshot_repr() -> None:
    """Test string representation of MetricSnapshot model."""
    recorded_time = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)
    snapshot = MetricSnapshot(
        id=1,
        domain_name="financials",
        metric_key="recurring_revenue",
        status_code="VALID",
        recorded_at=recorded_time,
    )
    expected_repr = (
        f"<MetricSnapshot(id=1, "
        f"domain_name='financials', "
        f"metric_key='recurring_revenue', "
        f"status_code='VALID', "
        f"recorded_at={recorded_time!r})>"
    )
    assert repr(snapshot) == expected_repr


def test_metric_snapshot_unique_constraint(test_db_session: Session) -> None:
    """Test that creating two snapshots with the same domain_name, metric_key, and recorded_at raises IntegrityError."""
    recorded_time = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)
    snapshot1 = MetricSnapshot(
        domain_name="sales_pipeline",
        metric_key="deal_value",
        current_value=50000.0,
        status_code="VALID",
        recorded_at=recorded_time,
    )
    test_db_session.add(snapshot1)
    test_db_session.commit()

    snapshot2 = MetricSnapshot(
        domain_name="sales_pipeline",
        metric_key="deal_value",
        current_value=60000.0,
        status_code="VALID",
        recorded_at=recorded_time,
    )
    test_db_session.add(snapshot2)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()


def test_metric_snapshot_not_null_constraints(test_db_session: Session) -> None:
    """Test that creating a snapshot missing required fields raises IntegrityError."""
    snapshot = MetricSnapshot(
        domain_name=None,  # type: ignore[arg-type]
        metric_key="some_key",
        status_code="VALID",
    )
    test_db_session.add(snapshot)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()
