"""Abstract port interfaces for Email, SMS, and In-App message dispatchers [FR-8].

This module defines the abstract port contracts and standardized execution DTOs for dispatching
financial transparency and alert notifications across Email, SMS, and In-App channels [FR-8].
Decouples core business logic, evaluation engines, and background workers from external
communication infrastructure (e.g., SMTP servers, SMS gateway providers, database persistence).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from src.models.alerting import (
    AlertUrgency,
    NotificationChannel,
    NotificationDeliveryStatus,
)


@dataclass
class EmailDispatchPayload:
    """Payload DTO containing parameters for email notification dispatch [FR-8]."""

    recipient_email: str
    subject: str
    body_text: str
    body_html: str | None = None
    sender_email: str | None = None
    urgency: str | AlertUrgency = AlertUrgency.MEDIUM.value
    correlation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EmailDispatchResult:
    """Standardized DTO representing the outcome of an email dispatch attempt [FR-8]."""

    success: bool
    recipient_email: str
    channel: str = NotificationChannel.EMAIL.value
    delivery_status: str = NotificationDeliveryStatus.SENT.value
    message_id: str | None = None
    dispatched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error_detail: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SMSDispatchPayload:
    """Payload DTO containing parameters for SMS notification dispatch [FR-8]."""

    phone_number: str
    message: str
    urgency: str | AlertUrgency = AlertUrgency.HIGH.value
    correlation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SMSDispatchResult:
    """Standardized DTO representing the outcome of an SMS dispatch attempt [FR-8]."""

    success: bool
    phone_number: str
    channel: str = NotificationChannel.SMS.value
    delivery_status: str = NotificationDeliveryStatus.SENT.value
    message_id: str | None = None
    dispatched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error_detail: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class InAppDispatchPayload:
    """Payload DTO containing parameters for in-app notification dispatch [FR-8]."""

    recipient_id: UUID | str | None
    title: str
    message: str
    urgency: str | AlertUrgency = AlertUrgency.MEDIUM.value
    trigger_id: UUID | str | None = None
    correlation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class InAppDispatchResult:
    """Standardized DTO representing the outcome of an in-app notification dispatch [FR-8]."""

    success: bool
    recipient_id: str | None
    channel: str = NotificationChannel.IN_APP.value
    delivery_status: str = NotificationDeliveryStatus.SENT.value
    notification_id: str | None = None
    dispatched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error_detail: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MultiChannelDispatchPayload:
    """Payload DTO for orchestrating alert dispatches across multiple channels [FR-8]."""

    recipient_id: UUID | str | None
    recipient_email: str | None
    phone_number: str | None
    title: str
    message: str
    urgency: str | AlertUrgency = AlertUrgency.MEDIUM.value
    trigger_id: UUID | str | None = None
    correlation_id: str | None = None
    channels: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MultiChannelDispatchResult:
    """Standardized DTO representing the consolidated outcomes of multi-channel dispatch [FR-8]."""

    success: bool
    results: dict[str, EmailDispatchResult | SMSDispatchResult | InAppDispatchResult] = field(
        default_factory=dict
    )
    dispatched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error_detail: str | None = None


class EmailDispatcherPort(ABC):
    """Abstract port interface for Email notification delivery [FR-8]."""

    @abstractmethod
    def send_email(self, payload: EmailDispatchPayload) -> EmailDispatchResult:
        """Dispatch an email notification to a recipient [FR-8].

        Args:
            payload: EmailDispatchPayload containing recipient, subject, body, and metadata.

        Returns:
            EmailDispatchResult representing the outcome of the email dispatch attempt.
        """
        ...

    def dispatch(
        self,
        recipient_email: str,
        subject: str,
        body_text: str,
        body_html: str | None = None,
        sender_email: str | None = None,
        urgency: str | AlertUrgency = AlertUrgency.MEDIUM.value,
        correlation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> EmailDispatchResult:
        """Convenience method to construct an EmailDispatchPayload and invoke send_email [FR-8]."""
        payload = EmailDispatchPayload(
            recipient_email=recipient_email,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            sender_email=sender_email,
            urgency=urgency,
            correlation_id=correlation_id,
            metadata=metadata or {},
        )
        return self.send_email(payload)


class SMSDispatcherPort(ABC):
    """Abstract port interface for SMS notification delivery [FR-8]."""

    @abstractmethod
    def send_sms(self, payload: SMSDispatchPayload) -> SMSDispatchResult:
        """Dispatch an SMS notification to a phone number [FR-8].

        Args:
            payload: SMSDispatchPayload containing phone number, message content, and metadata.

        Returns:
            SMSDispatchResult representing the outcome of the SMS dispatch attempt.
        """
        ...

    def dispatch(
        self,
        phone_number: str,
        message: str,
        urgency: str | AlertUrgency = AlertUrgency.HIGH.value,
        correlation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SMSDispatchResult:
        """Convenience method to construct an SMSDispatchPayload and invoke send_sms [FR-8]."""
        payload = SMSDispatchPayload(
            phone_number=phone_number,
            message=message,
            urgency=urgency,
            correlation_id=correlation_id,
            metadata=metadata or {},
        )
        return self.send_sms(payload)


class InAppDispatcherPort(ABC):
    """Abstract port interface for In-App notification delivery and persistence [FR-8]."""

    @abstractmethod
    def send_in_app(self, payload: InAppDispatchPayload) -> InAppDispatchResult:
        """Persist/dispatch an in-app notification for a user or role [FR-8].

        Args:
            payload: InAppDispatchPayload containing recipient ID, title, message, and metadata.

        Returns:
            InAppDispatchResult representing the outcome of the in-app dispatch operation.
        """
        ...

    def dispatch(
        self,
        recipient_id: UUID | str | None,
        title: str,
        message: str,
        urgency: str | AlertUrgency = AlertUrgency.MEDIUM.value,
        trigger_id: UUID | str | None = None,
        correlation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> InAppDispatchResult:
        """Convenience method to construct an InAppDispatchPayload and invoke send_in_app [FR-8]."""
        payload = InAppDispatchPayload(
            recipient_id=recipient_id,
            title=title,
            message=message,
            urgency=urgency,
            trigger_id=trigger_id,
            correlation_id=correlation_id,
            metadata=metadata or {},
        )
        return self.send_in_app(payload)


class MultiChannelDispatcherPort(ABC):
    """Abstract port interface for multi-channel alert dispatch orchestration [FR-8]."""

    @abstractmethod
    def dispatch_multi_channel(
        self, payload: MultiChannelDispatchPayload
    ) -> MultiChannelDispatchResult:
        """Orchestrate dispatching an alert across configured channels based on urgency [FR-8].

        Args:
            payload: MultiChannelDispatchPayload containing notification channels and details.

        Returns:
            MultiChannelDispatchResult with channel-specific execution outcomes.
        """
        ...


# Type aliases for alternative naming conventions
EmailPort = EmailDispatcherPort
SMSPort = SMSDispatcherPort
InAppPort = InAppDispatcherPort
