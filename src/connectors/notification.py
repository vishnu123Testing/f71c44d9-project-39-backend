"""Notification connector adapter for SLA breach warnings and tier escalation alerts [FR-5, FR-6, NFR-3, NFR-4, NFR-5].

This module provides the NotificationPort abstract interface and concrete notification adapters
(InAppNotificationAdapter, WebhookNotificationAdapter, NotificationConnector) for dispatching
proactive breach warnings, breach alerts, and escalation pathway notifications across configured channels.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from src.lib.config import settings
from src.lib.logging import get_correlation_id

logger = logging.getLogger(__name__)


class NotificationType(str, Enum):
    """Supported notification alert types."""

    BREACH_WARNING = "breach_warning"
    BREACH_ALERT = "breach_alert"
    ESCALATION_ALERT = "escalation_alert"
    STATUS_CHANGE = "status_change"


class NotificationChannel(str, Enum):
    """Supported notification delivery channels."""

    IN_APP = "in_app"
    EMAIL = "email"
    WEBHOOK = "webhook"


@dataclass
class NotificationPayload:
    """Dataclass encapsulating support notification event payload [NFR-3]."""

    ticket_id: UUID | str
    client_id: str
    notification_type: NotificationType | str
    priority: str
    subject: str
    message: str
    recipient_roles: list[str] = field(default_factory=list)
    recipient_emails: list[str] = field(default_factory=list)
    source_tier: str | None = None
    target_tier: str | None = None
    target_type: str | None = None  # "first_response" or "resolution"
    target_due_at: datetime | None = None
    breached_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class NotificationResult:
    """Dataclass encapsulating notification dispatch execution result."""

    success: bool
    notification_id: str
    channel: str
    dispatched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error_message: str | None = None
    recipient_count: int = 0
    details: dict[str, Any] = field(default_factory=dict)


class NotificationPort(ABC):
    """Abstract notification port definition for support operations [NFR-4]."""

    @abstractmethod
    def dispatch(self, payload: NotificationPayload) -> NotificationResult:
        """Dispatch a support notification payload to configured recipients.

        Args:
            payload: NotificationPayload containing ticket details and alert message.

        Returns:
            NotificationResult indicating success status and delivery details.
        """
        ...

    @abstractmethod
    def send_breach_warning(
        self,
        ticket_id: UUID | str,
        client_id: str,
        priority: str,
        target_type: str,
        target_due_at: datetime,
        remaining_minutes: float,
        recipient_roles: list[str] | None = None,
        subject: str | None = None,
        message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> NotificationResult:
        """Issue proactive breach notification when SLA timer crosses warning threshold [FR-5].

        Args:
            ticket_id: UUID or string ID of affected support ticket.
            client_id: Identifier of client owning the ticket.
            priority: Priority tier ("P1", "P2", "P3").
            target_type: SLA target type ("first_response" or "resolution").
            target_due_at: Target deadline timestamp.
            remaining_minutes: Estimated minutes remaining before breach.
            recipient_roles: List of target recipient roles.
            subject: Optional custom alert subject line.
            message: Optional custom notification message.
            metadata: Optional additional contextual attributes.

        Returns:
            NotificationResult detailing dispatch outcome.
        """
        ...

    @abstractmethod
    def send_breach_alert(
        self,
        ticket_id: UUID | str,
        client_id: str,
        priority: str,
        target_type: str,
        breached_at: datetime,
        recipient_roles: list[str] | None = None,
        subject: str | None = None,
        message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> NotificationResult:
        """Issue breach alert when SLA target deadline has passed [FR-5].

        Args:
            ticket_id: UUID or string ID of affected support ticket.
            client_id: Identifier of client owning the ticket.
            priority: Priority tier ("P1", "P2", "P3").
            target_type: SLA target type ("first_response" or "resolution").
            breached_at: Timestamp when SLA was breached.
            recipient_roles: List of target recipient roles.
            subject: Optional custom alert subject line.
            message: Optional custom notification message.
            metadata: Optional additional contextual attributes.

        Returns:
            NotificationResult detailing dispatch outcome.
        """
        ...

    @abstractmethod
    def send_escalation_alert(
        self,
        ticket_id: UUID | str,
        client_id: str,
        priority: str,
        source_tier: str,
        target_tier: str,
        reason: str,
        recipient_roles: list[str] | None = None,
        subject: str | None = None,
        message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> NotificationResult:
        """Execute escalation pathway alert notifying escalation targets [FR-6].

        Args:
            ticket_id: UUID or string ID of affected support ticket.
            client_id: Identifier of client owning the ticket.
            priority: Priority tier ("P1", "P2", "P3").
            source_tier: Initial tier prior to escalation (e.g., "Tier 1").
            target_tier: Target tier following escalation (e.g., "Tier 2").
            reason: Explanation for escalation action.
            recipient_roles: List of target recipient roles.
            subject: Optional custom alert subject line.
            message: Optional custom notification message.
            metadata: Optional additional contextual attributes.

        Returns:
            NotificationResult detailing dispatch outcome.
        """
        ...


class BaseNotificationAdapter(NotificationPort):
    """Base notification adapter implementing convenience dispatch builders [FR-5, FR-6]."""

    def send_breach_warning(
        self,
        ticket_id: UUID | str,
        client_id: str,
        priority: str,
        target_type: str,
        target_due_at: datetime,
        remaining_minutes: float,
        recipient_roles: list[str] | None = None,
        subject: str | None = None,
        message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> NotificationResult:
        """Construct breach warning payload and dispatch [FR-5]."""
        roles = recipient_roles or getattr(settings, "ESCALATION_RECIPIENT_ROLES", ["SalesManager", "Admin"])
        default_subject = f"SLA Warning: Ticket {ticket_id} ({priority}) approaching {target_type} deadline"
        default_message = (
            f"Ticket {ticket_id} for client {client_id} has {remaining_minutes:.1f} minutes remaining "
            f"before crossing the {target_type} SLA deadline at {target_due_at.isoformat()}."
        )

        payload = NotificationPayload(
            ticket_id=ticket_id,
            client_id=client_id,
            notification_type=NotificationType.BREACH_WARNING,
            priority=priority,
            subject=subject or default_subject,
            message=message or default_message,
            recipient_roles=roles,
            target_type=target_type,
            target_due_at=target_due_at,
            metadata=metadata or {},
        )
        return self.dispatch(payload)

    def send_breach_alert(
        self,
        ticket_id: UUID | str,
        client_id: str,
        priority: str,
        target_type: str,
        breached_at: datetime,
        recipient_roles: list[str] | None = None,
        subject: str | None = None,
        message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> NotificationResult:
        """Construct breach alert payload and dispatch [FR-5]."""
        roles = recipient_roles or getattr(settings, "ESCALATION_RECIPIENT_ROLES", ["SalesManager", "Admin"])
        default_subject = f"SLA BREACH: Ticket {ticket_id} ({priority}) breached {target_type} SLA"
        default_message = (
            f"CRITICAL: Ticket {ticket_id} for client {client_id} breached {target_type} SLA "
            f"at {breached_at.isoformat()}."
        )

        payload = NotificationPayload(
            ticket_id=ticket_id,
            client_id=client_id,
            notification_type=NotificationType.BREACH_ALERT,
            priority=priority,
            subject=subject or default_subject,
            message=message or default_message,
            recipient_roles=roles,
            target_type=target_type,
            breached_at=breached_at,
            metadata=metadata or {},
        )
        return self.dispatch(payload)

    def send_escalation_alert(
        self,
        ticket_id: UUID | str,
        client_id: str,
        priority: str,
        source_tier: str,
        target_tier: str,
        reason: str,
        recipient_roles: list[str] | None = None,
        subject: str | None = None,
        message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> NotificationResult:
        """Construct escalation alert payload and dispatch [FR-6]."""
        roles = recipient_roles or getattr(settings, "ESCALATION_RECIPIENT_ROLES", ["SalesManager", "Admin"])
        default_subject = f"ESCALATION: Ticket {ticket_id} escalated from {source_tier} to {target_tier}"
        default_message = (
            f"Ticket {ticket_id} ({priority}) for client {client_id} has been escalated from "
            f"{source_tier} to {target_tier}. Reason: {reason}."
        )

        payload = NotificationPayload(
            ticket_id=ticket_id,
            client_id=client_id,
            notification_type=NotificationType.ESCALATION_ALERT,
            priority=priority,
            subject=subject or default_subject,
            message=message or default_message,
            recipient_roles=roles,
            source_tier=source_tier,
            target_tier=target_tier,
            metadata=metadata or {},
        )
        return self.dispatch(payload)


class InAppNotificationAdapter(BaseNotificationAdapter):
    """In-app notification adapter recording notifications in-memory and issuing structured logs [NFR-3]."""

    def __init__(self) -> None:
        """Initialize in-app notification adapter."""
        self.sent_notifications: list[dict[str, Any]] = []

    def dispatch(self, payload: NotificationPayload) -> NotificationResult:
        """Dispatch in-app notification and emit structured JSON log [NFR-3]."""
        notification_id = f"notif-{uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)
        correlation_id = payload.correlation_id or get_correlation_id()

        record = {
            "notification_id": notification_id,
            "channel": NotificationChannel.IN_APP.value,
            "ticket_id": str(payload.ticket_id),
            "client_id": payload.client_id,
            "notification_type": str(payload.notification_type),
            "priority": payload.priority,
            "subject": payload.subject,
            "message": payload.message,
            "recipient_roles": payload.recipient_roles,
            "source_tier": payload.source_tier,
            "target_tier": payload.target_tier,
            "target_type": payload.target_type,
            "target_due_at": payload.target_due_at.isoformat() if payload.target_due_at else None,
            "breached_at": payload.breached_at.isoformat() if payload.breached_at else None,
            "dispatched_at": now.isoformat(),
            "correlation_id": correlation_id,
            "metadata": payload.metadata,
        }
        self.sent_notifications.append(record)

        # Emit structured machine-readable JSON log [NFR-3]
        structured_log = {
            "event": "support_notification_dispatched",
            "notification_id": notification_id,
            "channel": NotificationChannel.IN_APP.value,
            "ticket_id": str(payload.ticket_id),
            "client_id": payload.client_id,
            "notification_type": str(payload.notification_type),
            "priority": payload.priority,
            "recipient_roles": payload.recipient_roles,
            "source_tier": payload.source_tier,
            "target_tier": payload.target_tier,
            "correlation_id": correlation_id,
            "dispatched_at": now.isoformat(),
        }
        logger.info(json.dumps(structured_log))

        return NotificationResult(
            success=True,
            notification_id=notification_id,
            channel=NotificationChannel.IN_APP.value,
            dispatched_at=now,
            recipient_count=len(payload.recipient_roles),
            details=record,
        )


class WebhookNotificationAdapter(BaseNotificationAdapter):
    """Webhook notification adapter posting JSON alert payloads to external endpoints [FR-5, FR-6]."""

    def __init__(self, webhook_url: str | None = None) -> None:
        """Initialize webhook notification adapter."""
        self.webhook_url = webhook_url or os.getenv("WEBHOOK_NOTIFICATION_URL")
        self.sent_notifications: list[dict[str, Any]] = []

    def dispatch(self, payload: NotificationPayload) -> NotificationResult:
        """Dispatch alert payload via HTTP POST webhook."""
        notification_id = f"whnotif-{uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)
        correlation_id = payload.correlation_id or get_correlation_id()

        record = {
            "notification_id": notification_id,
            "channel": NotificationChannel.WEBHOOK.value,
            "ticket_id": str(payload.ticket_id),
            "client_id": payload.client_id,
            "notification_type": str(payload.notification_type),
            "priority": payload.priority,
            "subject": payload.subject,
            "message": payload.message,
            "recipient_roles": payload.recipient_roles,
            "source_tier": payload.source_tier,
            "target_tier": payload.target_tier,
            "dispatched_at": now.isoformat(),
            "correlation_id": correlation_id,
        }
        self.sent_notifications.append(record)

        if not self.webhook_url:
            logger.info("Webhook URL not configured; simulated webhook notification recorded.")
            return NotificationResult(
                success=True,
                notification_id=notification_id,
                channel=NotificationChannel.WEBHOOK.value,
                dispatched_at=now,
                recipient_count=len(payload.recipient_roles),
                details=record,
            )

        req_payload = json.dumps(record).encode("utf-8")
        req = urllib.request.Request(
            self.webhook_url,
            data=req_payload,
            headers={"Content-Type": "application/json", "X-Correlation-ID": correlation_id},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                status_code = resp.getcode()
                success = 200 <= status_code < 300
                return NotificationResult(
                    success=success,
                    notification_id=notification_id,
                    channel=NotificationChannel.WEBHOOK.value,
                    dispatched_at=now,
                    recipient_count=len(payload.recipient_roles),
                    details={"status_code": status_code, **record},
                )
        except Exception as exc:
            error_msg = f"Webhook notification failed: {exc!s}"
            logger.error(error_msg)
            return NotificationResult(
                success=False,
                notification_id=notification_id,
                channel=NotificationChannel.WEBHOOK.value,
                dispatched_at=now,
                error_message=error_msg,
                details=record,
            )


class NotificationConnector(BaseNotificationAdapter):
    """Primary notification connector adapter implementing NotificationPort for SLA alerts [FR-5, FR-6, NFR-3, NFR-4]."""

    def __init__(
        self,
        db: Session | None = None,
        channels: list[str] | str | None = None,
        notification_enabled: bool | None = None,
    ) -> None:
        """Initialize primary notification connector adapter.

        Args:
            db: Optional SQLAlchemy Session for optional persistent recording.
            channels: Target notification channel(s) (e.g. ["in_app", "webhook"]).
            notification_enabled: Master toggle for notification dispatch.
        """
        self.db = db
        raw_channels = channels or getattr(settings, "ALERT_NOTIFICATION_CHANNEL", "in_app")
        if isinstance(raw_channels, str):
            self.channels = [c.strip() for c in raw_channels.split(",") if c.strip()]
        else:
            self.channels = list(raw_channels)

        self.notification_enabled = (
            notification_enabled
            if notification_enabled is not None
            else getattr(settings, "NOTIFICATION_DISPATCH_ENABLED", True)
        )

        # Initialize sub-adapters
        self.in_app_adapter = InAppNotificationAdapter()
        self.webhook_adapter = WebhookNotificationAdapter()
        self.sent_notifications: list[dict[str, Any]] = []

    def dispatch(self, payload: NotificationPayload) -> NotificationResult:
        """Dispatch notification across configured active channels [NFR-3, NFR-4, NFR-5]."""
        if not self.notification_enabled:
            logger.warning(
                f"Notification dispatch is disabled in settings. Suppressing alert for ticket {payload.ticket_id}."
            )
            return NotificationResult(
                success=True,
                notification_id=f"suppressed-{uuid4().hex[:8]}",
                channel="disabled",
                dispatched_at=datetime.now(timezone.utc),
                details={"suppressed": True},
            )

        results: list[NotificationResult] = []
        for ch in self.channels:
            if ch == NotificationChannel.IN_APP.value or ch == "in_app":
                res = self.in_app_adapter.dispatch(payload)
                results.append(res)
            elif ch == NotificationChannel.WEBHOOK.value or ch == "webhook":
                res = self.webhook_adapter.dispatch(payload)
                results.append(res)

        if not results:
            # Default fallback to in-app
            res = self.in_app_adapter.dispatch(payload)
            results.append(res)

        primary_result = results[0]
        self.sent_notifications.extend(self.in_app_adapter.sent_notifications)

        # Emit aggregate structured JSON log
        structured_log = {
            "event": "support_notification_connector_dispatch",
            "ticket_id": str(payload.ticket_id),
            "client_id": payload.client_id,
            "notification_type": str(payload.notification_type),
            "priority": payload.priority,
            "channels_dispatched": [r.channel for r in results],
            "correlation_id": payload.correlation_id or get_correlation_id(),
            "dispatched_at": primary_result.dispatched_at.isoformat(),
        }
        logger.info(json.dumps(structured_log))

        return primary_result


def get_notification_connector(
    db: Session | None = None,
    channels: list[str] | str | None = None,
    notification_enabled: bool | None = None,
) -> NotificationConnector:
    """Factory function returning a configured NotificationConnector adapter instance [NFR-4]."""
    return NotificationConnector(
        db=db,
        channels=channels,
        notification_enabled=notification_enabled,
    )


# Alias for clarity
SupportNotificationConnector = NotificationConnector
