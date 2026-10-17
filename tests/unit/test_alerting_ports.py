"""Unit tests for alerting port abstract interfaces and DTOs [FR-8]."""

from uuid import uuid4

import pytest

from src.models.alerting import AlertUrgency, NotificationChannel, NotificationDeliveryStatus
from src.services.alerting.ports import (
    EmailDispatcherPort,
    EmailDispatchPayload,
    EmailDispatchResult,
    EmailPort,
    InAppDispatcherPort,
    InAppDispatchPayload,
    InAppDispatchResult,
    InAppPort,
    MultiChannelDispatcherPort,
    MultiChannelDispatchPayload,
    MultiChannelDispatchResult,
    SMSDispatcherPort,
    SMSDispatchPayload,
    SMSDispatchResult,
    SMSPort,
)


class MockEmailDispatcher(EmailDispatcherPort):
    """Concrete mock implementation of EmailDispatcherPort for testing."""

    def __init__(self, should_succeed: bool = True):
        self.should_succeed = should_succeed
        self.last_payload: EmailDispatchPayload | None = None

    def send_email(self, payload: EmailDispatchPayload) -> EmailDispatchResult:
        self.last_payload = payload
        if not self.should_succeed:
            return EmailDispatchResult(
                success=False,
                recipient_email=payload.recipient_email,
                delivery_status=NotificationDeliveryStatus.FAILED.value,
                error_detail="SMTP connection failed",
            )
        return EmailDispatchResult(
            success=True,
            recipient_email=payload.recipient_email,
            message_id="msg-123",
            delivery_status=NotificationDeliveryStatus.SENT.value,
        )


class MockSMSDispatcher(SMSDispatcherPort):
    """Concrete mock implementation of SMSDispatcherPort for testing."""

    def __init__(self, should_succeed: bool = True):
        self.should_succeed = should_succeed
        self.last_payload: SMSDispatchPayload | None = None

    def send_sms(self, payload: SMSDispatchPayload) -> SMSDispatchResult:
        self.last_payload = payload
        if not self.should_succeed:
            return SMSDispatchResult(
                success=False,
                phone_number=payload.phone_number,
                delivery_status=NotificationDeliveryStatus.FAILED.value,
                error_detail="SMS gateway timeout",
            )
        return SMSDispatchResult(
            success=True,
            phone_number=payload.phone_number,
            message_id="sms-456",
            delivery_status=NotificationDeliveryStatus.SENT.value,
        )


class MockInAppDispatcher(InAppDispatcherPort):
    """Concrete mock implementation of InAppDispatcherPort for testing."""

    def __init__(self, should_succeed: bool = True):
        self.should_succeed = should_succeed
        self.last_payload: InAppDispatchPayload | None = None

    def send_in_app(self, payload: InAppDispatchPayload) -> InAppDispatchResult:
        self.last_payload = payload
        recipient_str = str(payload.recipient_id) if payload.recipient_id else None
        if not self.should_succeed:
            return InAppDispatchResult(
                success=False,
                recipient_id=recipient_str,
                delivery_status=NotificationDeliveryStatus.FAILED.value,
                error_detail="Database transaction error",
            )
        return InAppDispatchResult(
            success=True,
            recipient_id=recipient_str,
            notification_id=str(uuid4()),
            delivery_status=NotificationDeliveryStatus.SENT.value,
        )


class MockMultiChannelDispatcher(MultiChannelDispatcherPort):
    """Concrete mock implementation of MultiChannelDispatcherPort for testing."""

    def dispatch_multi_channel(
        self, payload: MultiChannelDispatchPayload
    ) -> MultiChannelDispatchResult:
        results = {}
        if "Email" in payload.channels and payload.recipient_email:
            results["Email"] = EmailDispatchResult(
                success=True,
                recipient_email=payload.recipient_email,
            )
        if "SMS" in payload.channels and payload.phone_number:
            results["SMS"] = SMSDispatchResult(
                success=True,
                phone_number=payload.phone_number,
            )
        if "InApp" in payload.channels:
            recipient_str = str(payload.recipient_id) if payload.recipient_id else None
            results["InApp"] = InAppDispatchResult(
                success=True,
                recipient_id=recipient_str,
            )

        return MultiChannelDispatchResult(
            success=all(r.success for r in results.values()),
            results=results,
        )


def test_email_dispatcher_port_send_email():
    dispatcher = MockEmailDispatcher(should_succeed=True)
    payload = EmailDispatchPayload(
        recipient_email="user@example.com",
        subject="Invoice Past Due",
        body_text="Your invoice #1001 is past due.",
        urgency=AlertUrgency.HIGH.value,
        correlation_id="corr-1",
    )
    result = dispatcher.send_email(payload)

    assert result.success is True
    assert result.recipient_email == "user@example.com"
    assert result.channel == NotificationChannel.EMAIL.value
    assert result.delivery_status == NotificationDeliveryStatus.SENT.value
    assert result.message_id == "msg-123"
    assert dispatcher.last_payload == payload


def test_email_dispatcher_port_convenience_dispatch():
    dispatcher = MockEmailDispatcher(should_succeed=True)
    result = dispatcher.dispatch(
        recipient_email="finance@example.com",
        subject="Payment Received",
        body_text="Payment of $500 received.",
    )

    assert result.success is True
    assert result.recipient_email == "finance@example.com"
    assert dispatcher.last_payload is not None
    assert dispatcher.last_payload.subject == "Payment Received"


def test_email_dispatcher_port_failure():
    dispatcher = MockEmailDispatcher(should_succeed=False)
    payload = EmailDispatchPayload(
        recipient_email="fail@example.com",
        subject="Test Failure",
        body_text="Testing failure scenario",
    )
    result = dispatcher.send_email(payload)

    assert result.success is False
    assert result.delivery_status == NotificationDeliveryStatus.FAILED.value
    assert result.error_detail == "SMTP connection failed"


def test_sms_dispatcher_port_send_sms():
    dispatcher = MockSMSDispatcher(should_succeed=True)
    payload = SMSDispatchPayload(
        phone_number="+15551234567",
        message="Urgent: Invoice over threshold",
        urgency=AlertUrgency.HIGH.value,
    )
    result = dispatcher.send_sms(payload)

    assert result.success is True
    assert result.phone_number == "+15551234567"
    assert result.channel == NotificationChannel.SMS.value
    assert result.delivery_status == NotificationDeliveryStatus.SENT.value
    assert result.message_id == "sms-456"


def test_sms_dispatcher_port_convenience_dispatch():
    dispatcher = MockSMSDispatcher(should_succeed=True)
    result = dispatcher.dispatch(
        phone_number="+15559876543",
        message="Low urgency update",
        urgency=AlertUrgency.LOW.value,
    )

    assert result.success is True
    assert result.phone_number == "+15559876543"
    assert dispatcher.last_payload is not None
    assert dispatcher.last_payload.urgency == AlertUrgency.LOW.value


def test_sms_dispatcher_port_failure():
    dispatcher = MockSMSDispatcher(should_succeed=False)
    result = dispatcher.dispatch(
        phone_number="+15550000000",
        message="Fail test",
    )

    assert result.success is False
    assert result.delivery_status == NotificationDeliveryStatus.FAILED.value
    assert result.error_detail == "SMS gateway timeout"


def test_in_app_dispatcher_port_send_in_app():
    dispatcher = MockInAppDispatcher(should_succeed=True)
    user_id = uuid4()
    trigger_id = uuid4()
    payload = InAppDispatchPayload(
        recipient_id=user_id,
        title="High Balance Alert",
        message="AR balance exceeds $10,000",
        urgency=AlertUrgency.HIGH.value,
        trigger_id=trigger_id,
    )
    result = dispatcher.send_in_app(payload)

    assert result.success is True
    assert result.recipient_id == str(user_id)
    assert result.channel == NotificationChannel.IN_APP.value
    assert result.notification_id is not None


def test_in_app_dispatcher_port_convenience_dispatch():
    dispatcher = MockInAppDispatcher(should_succeed=True)
    user_id = uuid4()
    result = dispatcher.dispatch(
        recipient_id=user_id,
        title="Monthly Financial Report",
        message="Your report is ready",
    )

    assert result.success is True
    assert result.recipient_id == str(user_id)
    assert dispatcher.last_payload is not None
    assert dispatcher.last_payload.title == "Monthly Financial Report"


def test_multi_channel_dispatcher_port():
    dispatcher = MockMultiChannelDispatcher()
    user_id = uuid4()
    payload = MultiChannelDispatchPayload(
        recipient_id=user_id,
        recipient_email="user@example.com",
        phone_number="+15551112222",
        title="High Urgency Alert",
        message="Invoices past 90 days",
        urgency=AlertUrgency.HIGH.value,
        channels=["InApp", "Email", "SMS"],
    )
    result = dispatcher.dispatch_multi_channel(payload)

    assert result.success is True
    assert "InApp" in result.results
    assert "Email" in result.results
    assert "SMS" in result.results


def test_type_aliases():
    assert EmailPort is EmailDispatcherPort
    assert SMSPort is SMSDispatcherPort
    assert InAppPort is InAppDispatcherPort


def test_abstract_class_instantiation_raises_type_error():
    with pytest.raises(TypeError):
        EmailDispatcherPort()  # type: ignore

    with pytest.raises(TypeError):
        SMSDispatcherPort()  # type: ignore

    with pytest.raises(TypeError):
        InAppDispatcherPort()  # type: ignore

    with pytest.raises(TypeError):
        MultiChannelDispatcherPort()  # type: ignore
