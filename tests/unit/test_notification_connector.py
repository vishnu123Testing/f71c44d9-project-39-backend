"""Unit tests for NotificationConnector renewal alert delivery and notification dispatch [FR-9]."""

from datetime import date as date_type, datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.connectors.notification_connector import (
    InAppNotificationConnector,
    NotificationConnector,
    RenewalNotificationConnector,
)
from src.models.contract import Contract, RenewalAlert
from src.models.database import Base
from src.services.contract.models import ContractCategory, ContractStatus


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


def test_notification_connector_aliases() -> None:
    """Verify class aliases point to NotificationConnector."""
    assert InAppNotificationConnector is NotificationConnector
    assert RenewalNotificationConnector is NotificationConnector


def test_calculate_days_until_expiration() -> None:
    """Verify expiration date calculation with date and datetime objects [FR-9]."""
    connector = NotificationConnector()

    ref_date = date_type(2026, 1, 1)
    exp_date = date_type(2026, 4, 1)  # 90 days later
    days_left = connector.calculate_days_until_expiration(expires_on=exp_date, reference_date=ref_date)
    assert days_left == 90

    exp_datetime = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    ref_datetime = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    days_left_dt = connector.calculate_days_until_expiration(expires_on=exp_datetime, reference_date=ref_datetime)
    assert days_left_dt == 90


def test_should_suppress_alert_rules() -> None:
    """Verify alert suppression rules for various contract statuses and dispatched thresholds [FR-9]."""
    connector = NotificationConnector()

    # Suppress terminated, expired, draft
    assert connector.should_suppress_alert(ContractStatus.TERMINATED) is True
    assert connector.should_suppress_alert("TERMINATED") is True
    assert connector.should_suppress_alert(ContractStatus.EXPIRED) is True
    assert connector.should_suppress_alert("EXPIRED") is True
    assert connector.should_suppress_alert(ContractStatus.DRAFT) is True
    assert connector.should_suppress_alert("DRAFT") is True

    # Active contracts are not suppressed by status
    assert connector.should_suppress_alert(ContractStatus.ACTIVE) is False
    assert connector.should_suppress_alert("ACTIVE") is False

    # Suppress if target threshold was already dispatched
    assert connector.should_suppress_alert(
        status=ContractStatus.ACTIVE,
        already_dispatched_thresholds=[90, 60],
        target_threshold_days=90,
    ) is True

    # Do not suppress if target threshold was not yet dispatched
    assert connector.should_suppress_alert(
        status=ContractStatus.ACTIVE,
        already_dispatched_thresholds=[90, 60],
        target_threshold_days=30,
    ) is False


def test_dispatch_renewal_alert_in_memory() -> None:
    """Verify renewal alert dispatching with in-memory fallback (no DB) [FR-9]."""
    connector = NotificationConnector()
    contract_id = uuid.uuid4()
    exp_date = date_type(2026, 6, 1)

    result_90 = connector.dispatch_renewal_alert(
        contract_id=contract_id,
        threshold_days=90,
        title="Software License Agreement",
        counterparty="TechCorp",
        expires_on=exp_date,
        recipients=["admin@techcorp.com"],
    )

    assert result_90.success is True
    assert result_90.suppressed is False
    assert result_90.threshold_days == 90
    assert result_90.contract_id == contract_id
    assert result_90.recipients == ["admin@techcorp.com"]

    # Verify history query
    alerts = connector.get_dispatched_alerts(contract_id)
    assert len(alerts) == 1
    assert alerts[0].threshold_days == 90

    # Attempting duplicate dispatch for same threshold should be suppressed
    result_dup = connector.dispatch_renewal_alert(
        contract_id=contract_id,
        threshold_days=90,
        title="Software License Agreement",
    )
    assert result_dup.suppressed is True
    assert result_dup.success is False

    # Dispatching for 60-day threshold should succeed
    result_60 = connector.dispatch_renewal_alert(
        contract_id=contract_id,
        threshold_days=60,
        title="Software License Agreement",
    )
    assert result_60.success is True
    assert result_60.suppressed is False
    assert len(connector.get_dispatched_alerts(contract_id)) == 2


def test_dispatch_renewal_alert_with_db(db_session: Session) -> None:
    """Verify renewal alert dispatching and persistence with a database session [FR-9]."""
    connector = NotificationConnector(db=db_session)
    contract_id = uuid.uuid4()
    exp_date = date_type(2026, 12, 31)

    contract = Contract(
        id=contract_id,
        title="Master Services Agreement",
        category=ContractCategory.CONTRACT.value,
        status=ContractStatus.ACTIVE.value,
        counterparty="Acme Services",
        expires_on=exp_date,
    )
    db_session.add(contract)
    db_session.commit()

    # Dispatch 90-day alert
    result = connector.dispatch_renewal_alert(
        contract_id=contract_id,
        threshold_days=90,
        recipients=["legal@acme.com"],
    )

    assert result.success is True
    assert result.suppressed is False

    # Check persistence in DB
    db_alerts = db_session.query(RenewalAlert).filter(RenewalAlert.contract_id == contract_id).all()
    assert len(db_alerts) == 1
    assert db_alerts[0].threshold_days == 90

    # Duplicate 90-day alert should be suppressed
    dup_result = connector.dispatch_renewal_alert(
        contract_id=contract_id,
        threshold_days=90,
    )
    assert dup_result.suppressed is True


def test_suppressed_if_contract_terminated_in_db(db_session: Session) -> None:
    """Verify alerts are suppressed if contract status in database is TERMINATED or EXPIRED [FR-9]."""
    connector = NotificationConnector(db=db_session)
    contract_id = uuid.uuid4()

    contract = Contract(
        id=contract_id,
        title="Terminated Contract",
        category=ContractCategory.NDA.value,
        status=ContractStatus.TERMINATED.value,
        counterparty="Beta Ltd",
    )
    db_session.add(contract)
    db_session.commit()

    result = connector.dispatch_renewal_alert(
        contract_id=contract_id,
        threshold_days=90,
    )

    assert result.suppressed is True
    assert result.success is False
    assert "TERMINATED" in (result.suppression_reason or "")


def test_send_general_notification() -> None:
    """Verify general notification delivery via send_notification [FR-9]."""
    connector = NotificationConnector()

    ok = connector.send_notification(
        recipient="user@example.com",
        subject="Alert Test",
        body="This is a test notification",
        channel="email",
    )

    assert ok is True
    assert len(connector._sent_notifications) == 1
    assert connector._sent_notifications[0]["recipient"] == "user@example.com"
    assert connector._sent_notifications[0]["channel"] == "email"
