"""Unit tests for notification background worker tasks, audit log recording, and SMS fallback execution [FR-8, FR-12, NFR-2, NFR-4]."""

from datetime import datetime, timezone
import logging
from unittest.mock import MagicMock, patch
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from src.connectors.email import SMTPEmailConnector
from src.connectors.in_app import InAppPersistenceConnector
from src.connectors.sms import SMSGatewayConnector
from src.models.alerting import (
    AlertUrgency,
    NotificationChannel,
    NotificationDeliveryStatus,
    NotificationLog,
    NotificationPreference,
)
from src.models.database import Base
from src.services.alerting.ports import (
    EmailDispatchPayload,
    EmailDispatchResult,
    InAppDispatchPayload,
    InAppDispatchResult,
    SMSDispatchPayload,
    SMSDispatchResult,
)
from src.workers.notification_tasks import (
    dispatch_alert_notification,
    run_notification_dispatch,
)


@pytest.fixture
def test_db_session() -> Session:
    """Fixture providing an isolated in-memory SQLite database session for testing."""
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


def test_medium_urgency_dispatch(test_db_session: Session) -> None:
    """Test Medium urgency alert dispatches to InApp and Email without SMS [FR-8, FR-12]."""
    mock_email = MagicMock()
    mock_email.send_email.return_value = EmailDispatchResult(
        success=True,
        recipient_email="user@example.com",
        delivery_status=NotificationDeliveryStatus.SENT.value,
    )

    mock_sms = MagicMock()

    recipient_id = uuid.uuid4()
    summary = run_notification_dispatch(
        db=test_db_session,
        recipient_id=recipient_id,
        recipient_email="user@example.com",
        title="Payment Received",
        message="Your invoice payment of $500.00 has been processed.",
        urgency="Medium",
        email_connector=mock_email,
        sms_connector=mock_sms,
    )

    # Medium urgency routes to InApp and Email
    mock_email.send_email.assert_called_once()
    mock_sms.send_sms.assert_not_called()

    # Verify audit logs in database
    logs = list(
        test_db_session.scalars(
            select(NotificationLog).where(NotificationLog.recipient_id == recipient_id)
        ).all()
    )
    assert len(logs) >= 2
    channels_logged = {l.channel for l in logs}
    assert NotificationChannel.IN_APP.value in channels_logged
    assert NotificationChannel.EMAIL.value in channels_logged
    assert NotificationChannel.SMS.value not in channels_logged


def test_high_urgency_multi_channel_dispatch(test_db_session: Session) -> None:
    """Test High urgency alert dispatches across InApp, Email, and SMS channels [FR-8, FR-12]."""
    mock_email = MagicMock()
    mock_email.send_email.return_value = EmailDispatchResult(
        success=True,
        recipient_email="finance@example.com",
        delivery_status=NotificationDeliveryStatus.SENT.value,
    )

    mock_sms = MagicMock()
    mock_sms.send_sms.return_value = SMSDispatchResult(
        success=True,
        phone_number="+15551234567",
        delivery_status=NotificationDeliveryStatus.SENT.value,
    )

    recipient_id = uuid.uuid4()
    summary = run_notification_dispatch(
        db=test_db_session,
        recipient_id=recipient_id,
        recipient_email="finance@example.com",
        recipient_phone="+15551234567",
        title="High Threshold Breach",
        message="Past due invoice balance exceeds $50,000 threshold.",
        urgency="High",
        email_connector=mock_email,
        sms_connector=mock_sms,
    )

    mock_email.send_email.assert_called_once()
    mock_sms.send_sms.assert_called_once()

    # Verify database audit logs
    logs = list(
        test_db_session.scalars(
            select(NotificationLog).where(NotificationLog.recipient_id == recipient_id)
        ).all()
    )
    channels_logged = {l.channel for l in logs}
    assert NotificationChannel.IN_APP.value in channels_logged
    assert NotificationChannel.EMAIL.value in channels_logged
    assert NotificationChannel.SMS.value in channels_logged


def test_sms_gateway_failure_and_fallback_execution(
    test_db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """Test SMS gateway failure logs structured warning and triggers Email/InApp fallback dispatches [FR-8, FR-12, NFR-2, NFR-4]."""
    mock_email = MagicMock()
    mock_email.send_email.return_value = EmailDispatchResult(
        success=True,
        recipient_email="admin@example.com",
        delivery_status=NotificationDeliveryStatus.SENT.value,
    )

    # SMS connector configured to simulate failure
    mock_sms = MagicMock()
    mock_sms.send_sms.return_value = SMSDispatchResult(
        success=False,
        phone_number="+15559999999",
        delivery_status=NotificationDeliveryStatus.FAILED.value,
        error_detail="SMS Gateway Timeout",
    )

    recipient_id = uuid.uuid4()
    corr_id = "test-correlation-12345"

    with caplog.at_level(logging.WARNING):
        summary = run_notification_dispatch(
            db=test_db_session,
            recipient_id=recipient_id,
            recipient_email="admin@example.com",
            recipient_phone="+15559999999",
            title="Urgent Past Due Alert",
            message="Critical invoice past due threshold reached.",
            urgency="High",
            correlation_id=corr_id,
            email_connector=mock_email,
            sms_connector=mock_sms,
        )

    # Verify structured warning log output contains correlation ID [NFR-2]
    assert "SMS gateway delivery failed" in caplog.text
    assert corr_id in caplog.text

    # Verify SMS log in DB has Fallback delivery status [FR-12]
    sms_logs = list(
        test_db_session.scalars(
            select(NotificationLog).where(
                NotificationLog.recipient_id == recipient_id,
                NotificationLog.channel == NotificationChannel.SMS.value,
            )
        ).all()
    )
    assert len(sms_logs) == 1
    assert sms_logs[0].delivery_status == NotificationDeliveryStatus.FALLBACK.value

    # Verify Email was called and logged
    mock_email.send_email.assert_called()


def test_missing_phone_number_triggers_sms_fallback(test_db_session: Session) -> None:
    """Test High urgency notification with missing phone number triggers SMS fallback handling [FR-8, FR-12, NFR-4]."""
    mock_email = MagicMock()
    mock_email.send_email.return_value = EmailDispatchResult(
        success=True,
        recipient_email="user@example.com",
        delivery_status=NotificationDeliveryStatus.SENT.value,
    )

    mock_sms = MagicMock()

    recipient_id = uuid.uuid4()
    summary = run_notification_dispatch(
        db=test_db_session,
        recipient_id=recipient_id,
        recipient_email="user@example.com",
        recipient_phone=None,  # Missing phone number
        title="High Alert",
        message="Critical notification text",
        urgency="High",
        email_connector=mock_email,
        sms_connector=mock_sms,
    )

    # SMS send is skipped or fails due to missing phone number
    mock_sms.send_sms.assert_not_called()

    # SMS log should be recorded as Fallback
    sms_logs = list(
        test_db_session.scalars(
            select(NotificationLog).where(
                NotificationLog.recipient_id == recipient_id,
                NotificationLog.channel == NotificationChannel.SMS.value,
            )
        ).all()
    )
    assert len(sms_logs) == 1
    assert sms_logs[0].delivery_status == NotificationDeliveryStatus.FALLBACK.value


def test_role_preference_channel_routing(test_db_session: Session) -> None:
    """Test channel routing respects role notification preferences from database [FR-7, FR-8]."""
    # Create role preference for 'financial_admin' to receive Email and SMS for 'Medium' urgency
    pref = NotificationPreference(
        role_name="financial_admin",
        event_type="invoice_overdue",
        urgency="Medium",
        channels_json=["Email", "SMS"],
        version=1,
    )
    test_db_session.add(pref)
    test_db_session.commit()

    mock_email = MagicMock()
    mock_email.send_email.return_value = EmailDispatchResult(
        success=True,
        recipient_email="finadmin@example.com",
        delivery_status=NotificationDeliveryStatus.SENT.value,
    )

    mock_sms = MagicMock()
    mock_sms.send_sms.return_value = SMSDispatchResult(
        success=True,
        phone_number="+15551112222",
        delivery_status=NotificationDeliveryStatus.SENT.value,
    )

    recipient_id = uuid.uuid4()
    summary = run_notification_dispatch(
        db=test_db_session,
        recipient_id=recipient_id,
        recipient_email="finadmin@example.com",
        recipient_phone="+15551112222",
        title="Invoice Overdue",
        message="Invoice #1001 is overdue.",
        urgency="Medium",
        role_name="financial_admin",
        event_type="invoice_overdue",
        email_connector=mock_email,
        sms_connector=mock_sms,
    )

    # Per role preference, Email and SMS were routed
    mock_email.send_email.assert_called_once()
    mock_sms.send_sms.assert_called_once()


def test_celery_task_execution(test_db_session: Session) -> None:
    """Test dispatch_alert_notification Celery task executes deterministically [FR-8, NFR-4]."""
    with patch("src.workers.notification_tasks.run_notification_dispatch") as mock_run:
        mock_run.return_value = {
            "recipient_id": "123",
            "title": "Alert",
            "urgency": "Medium",
            "dispatched_logs": [],
            "correlation_id": "test-corr",
        }

        # Invoke the task directly
        result = dispatch_alert_notification(
            recipient_id="123",
            title="Alert",
            message="Message",
            urgency="Medium",
        )

        assert result["recipient_id"] == "123"
        mock_run.assert_called_once()
