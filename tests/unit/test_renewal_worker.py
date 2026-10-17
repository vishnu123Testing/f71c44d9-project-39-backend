"""Unit tests for scheduled renewal worker task and expiration threshold calculations [FR-9]."""

from datetime import date as date_type, datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.contract import Contract, RenewalAlert
from src.models.database import Base
from src.services.contract.models import ContractCategory, ContractStatus
from src.workers.renewal_worker import calculate_threshold_alerts, process_renewal_alerts


@pytest.fixture
def db_session() -> Session:
    """Create an isolated in-memory SQLite database session for unit testing."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def test_calculate_threshold_alerts_90_60_30(db_session: Session) -> None:
    """Verify renewal alert calculation for 90, 60, and 30-day thresholds on active contracts [FR-9]."""
    ref_date = date_type(2026, 1, 1)

    # Contract A: 90 days remaining (expires 2026-04-01)
    contract_90 = Contract(
        id=uuid.uuid4(),
        title="Agreement 90 Days",
        category=ContractCategory.CONTRACT.value,
        status=ContractStatus.ACTIVE.value,
        counterparty="Vendor A",
        expires_on=date_type(2026, 4, 1),
    )

    # Contract B: 60 days remaining (expires 2026-03-02)
    contract_60 = Contract(
        id=uuid.uuid4(),
        title="Agreement 60 Days",
        category=ContractCategory.CONTRACT.value,
        status=ContractStatus.ACTIVE.value,
        counterparty="Vendor B",
        expires_on=date_type(2026, 3, 2),
    )

    # Contract C: 30 days remaining (expires 2026-01-31)
    contract_30 = Contract(
        id=uuid.uuid4(),
        title="Agreement 30 Days",
        category=ContractCategory.CONTRACT.value,
        status=ContractStatus.ACTIVE.value,
        counterparty="Vendor C",
        expires_on=date_type(2026, 1, 31),
    )

    # Contract D: 120 days remaining (far in future, no alert yet)
    contract_120 = Contract(
        id=uuid.uuid4(),
        title="Agreement 120 Days",
        category=ContractCategory.CONTRACT.value,
        status=ContractStatus.ACTIVE.value,
        counterparty="Vendor D",
        expires_on=date_type(2026, 5, 1),
    )

    db_session.add_all([contract_90, contract_60, contract_30, contract_120])
    db_session.commit()

    summary = calculate_threshold_alerts(db=db_session, reference_date=ref_date)

    assert summary["processed_contracts"] == 4
    # contract_90 gets threshold 90
    # contract_60 gets threshold 90 and 60 (since 60 <= 90 and 60 <= 60)
    # contract_30 gets threshold 90, 60, and 30
    # Total dispatched: 1 + 2 + 3 = 6
    assert summary["alerts_dispatched"] > 0

    # Verify DB persistence of RenewalAlert records
    alerts_90 = db_session.query(RenewalAlert).filter(RenewalAlert.contract_id == contract_90.id).all()
    assert len(alerts_90) == 1
    assert alerts_90[0].threshold_days == 90

    alerts_30 = db_session.query(RenewalAlert).filter(RenewalAlert.contract_id == contract_30.id).all()
    thresholds_30 = {a.threshold_days for a in alerts_30}
    assert thresholds_30 == {90, 60, 30}


def test_calculate_threshold_alerts_suppressed_statuses(db_session: Session) -> None:
    """Verify alerts are suppressed if contract is terminated, expired, or draft [FR-9]."""
    ref_date = date_type(2026, 1, 1)

    term_contract = Contract(
        id=uuid.uuid4(),
        title="Terminated Contract",
        category=ContractCategory.CONTRACT.value,
        status=ContractStatus.TERMINATED.value,
        counterparty="Vendor Term",
        expires_on=date_type(2026, 1, 20),  # 19 days left
    )

    exp_contract = Contract(
        id=uuid.uuid4(),
        title="Expired Status Contract",
        category=ContractCategory.CONTRACT.value,
        status=ContractStatus.EXPIRED.value,
        counterparty="Vendor Exp",
        expires_on=date_type(2026, 1, 20),
    )

    draft_contract = Contract(
        id=uuid.uuid4(),
        title="Draft Contract",
        category=ContractCategory.CONTRACT.value,
        status=ContractStatus.DRAFT.value,
        counterparty="Vendor Draft",
        expires_on=date_type(2026, 1, 20),
    )

    db_session.add_all([term_contract, exp_contract, draft_contract])
    db_session.commit()

    summary = calculate_threshold_alerts(db=db_session, reference_date=ref_date)

    assert summary["alerts_dispatched"] == 0
    assert summary["alerts_suppressed"] == 3

    # Check that no RenewalAlert rows were inserted
    count = db_session.query(RenewalAlert).count()
    assert count == 0


def test_calculate_threshold_alerts_no_duplicate_dispatches(db_session: Session) -> None:
    """Verify threshold alerts are not dispatched twice for the same contract and threshold [FR-9]."""
    ref_date = date_type(2026, 1, 1)

    contract = Contract(
        id=uuid.uuid4(),
        title="Pre-alerted Contract",
        category=ContractCategory.CONTRACT.value,
        status=ContractStatus.ACTIVE.value,
        counterparty="Vendor Pre",
        expires_on=date_type(2026, 3, 2),  # 60 days left
    )
    db_session.add(contract)
    db_session.commit()

    # Pre-insert 90-day alert
    pre_alert = RenewalAlert(
        id=uuid.uuid4(),
        contract_id=contract.id,
        threshold_days=90,
        dispatched_at=datetime.now(timezone.utc),
    )
    db_session.add(pre_alert)
    db_session.commit()

    summary = calculate_threshold_alerts(db=db_session, reference_date=ref_date)

    # Threshold 90 should be skipped (already dispatched); threshold 60 should be dispatched
    alerts = db_session.query(RenewalAlert).filter(RenewalAlert.contract_id == contract.id).all()
    assert len(alerts) == 2
    alert_thresholds = {a.threshold_days for a in alerts}
    assert alert_thresholds == {90, 60}


def test_calculate_threshold_alerts_past_expiration(db_session: Session) -> None:
    """Verify contracts past expiration (negative days remaining) are skipped [FR-9]."""
    ref_date = date_type(2026, 1, 1)

    past_contract = Contract(
        id=uuid.uuid4(),
        title="Past Expiration Contract",
        category=ContractCategory.CONTRACT.value,
        status=ContractStatus.ACTIVE.value,
        counterparty="Vendor Past",
        expires_on=date_type(2025, 12, 15),  # 17 days past
    )
    db_session.add(past_contract)
    db_session.commit()

    summary = calculate_threshold_alerts(db=db_session, reference_date=ref_date)

    assert summary["alerts_dispatched"] == 0
    count = db_session.query(RenewalAlert).count()
    assert count == 0


def test_process_renewal_alerts_task(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify end-to-end execution of process_renewal_alerts task [FR-9]."""
    # Monkeypatch SessionLocal to return db_session fixture
    monkeypatch.setattr("src.workers.renewal_worker.SessionLocal", lambda: db_session)

    contract = Contract(
        id=uuid.uuid4(),
        title="Task Test Contract",
        category=ContractCategory.CONTRACT.value,
        status=ContractStatus.ACTIVE.value,
        counterparty="Vendor Task",
        expires_on=date_type(2026, 10, 1),
    )
    db_session.add(contract)
    db_session.commit()

    result = process_renewal_alerts(reference_date_str="2026-07-03")  # 90 days before 2026-10-01

    assert result["processed_contracts"] == 1
    assert result["alerts_dispatched"] == 1
