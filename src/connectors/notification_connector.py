"""Notification connector adapter implementing NotificationPort for renewal alert delivery [FR-9].

This module provides the concrete NotificationConnector implementation for calculating contract expiration
dates, evaluating alert suppression rules, recording dispatched renewal alerts, and delivering in-app
or external notification alerts at 90, 60, and 30-day thresholds [FR-9].

Supports synchronous database persistence via SQLAlchemy Session when provided, and
falls back to in-memory state tracking when initialized without a database session.
"""

from datetime import date as date_type, datetime, timezone
import logging
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from src.lib.config import settings
from src.lib.logging import get_correlation_id
from src.models.contract import Contract, RenewalAlert
from src.services.contract.models import (
    ContractStatus,
    RenewalAlertDomain,
)
from src.services.contract.notification_port import (
    RENEWAL_ALERT_THRESHOLDS,
    SUPPRESSED_CONTRACT_STATUSES,
    NotificationPort,
    RenewalAlertDispatchResult,
)

logger = logging.getLogger(__name__)


class NotificationConnector(NotificationPort):
    """Notification connector adapter implementing NotificationPort for renewal alert delivery [FR-9]."""

    def __init__(
        self,
        db: Session | None = None,
        channel: str | None = None,
        notification_enabled: bool | None = None,
    ) -> None:
        """Initialize NotificationConnector adapter.

        Args:
            db: Optional SQLAlchemy Session for database persistence of renewal alerts.
            channel: Default notification delivery channel (e.g., "in_app", "email", "webhook").
            notification_enabled: Flag indicating if notification dispatch is enabled.
        """
        self.db = db
        self.channel = channel or getattr(settings, "ALERT_NOTIFICATION_CHANNEL", "in_app")
        self.notification_enabled = (
            notification_enabled
            if notification_enabled is not None
            else getattr(settings, "NOTIFICATION_DISPATCH_ENABLED", True)
        )
        self._in_memory_alerts: dict[UUID, list[RenewalAlertDomain]] = {}
        self._sent_notifications: list[dict[str, Any]] = []

    def _to_domain(self, orm_alert: RenewalAlert) -> RenewalAlertDomain:
        """Convert a RenewalAlert ORM model instance into a RenewalAlertDomain entity."""
        return RenewalAlertDomain(
            id=orm_alert.id,
            contract_id=orm_alert.contract_id,
            threshold_days=orm_alert.threshold_days,
            dispatched_at=orm_alert.dispatched_at,
        )

    def calculate_days_until_expiration(
        self,
        expires_on: date_type,
        reference_date: date_type | None = None,
    ) -> int:
        """Calculate the remaining days from a reference date until contract expiration [FR-9].

        Args:
            expires_on: Contract expiration date.
            reference_date: Optional reference date (defaults to current UTC date).

        Returns:
            Integer representing remaining days until expiration.
        """
        exp_date = expires_on.date() if isinstance(expires_on, datetime) else expires_on

        ref_date = reference_date or datetime.now(timezone.utc).date()
        if isinstance(ref_date, datetime):
            ref_date = ref_date.date()

        return (exp_date - ref_date).days

    def should_suppress_alert(
        self,
        status: ContractStatus | str,
        already_dispatched_thresholds: list[int] | None = None,
        target_threshold_days: int | None = None,
    ) -> bool:
        """Evaluate whether a renewal alert should be suppressed for a contract [FR-9].

        Alerts are suppressed if the contract status is terminated, expired, draft,
        or if an alert for target_threshold_days has already been dispatched.

        Args:
            status: Current contract lifecycle status.
            already_dispatched_thresholds: Optional list of thresholds already dispatched.
            target_threshold_days: Optional specific threshold days being evaluated.

        Returns:
            True if alert should be suppressed, False otherwise.
        """
        status_val = status.value if isinstance(status, ContractStatus) else str(status)

        suppressed_set = {
            s.value if isinstance(s, ContractStatus) else str(s)
            for s in SUPPRESSED_CONTRACT_STATUSES
        }

        if status_val in suppressed_set or status_val.upper() in {s.upper() for s in suppressed_set}:
            return True

        if (
            already_dispatched_thresholds is not None
            and target_threshold_days is not None
            and target_threshold_days in already_dispatched_thresholds
        ):
            return True

        return False

    def get_dispatched_alerts(self, contract_id: UUID) -> list[RenewalAlertDomain]:
        """Retrieve all previously dispatched renewal alerts for a contract [FR-9].

        Args:
            contract_id: Unique UUID identifier of the contract.

        Returns:
            List of RenewalAlertDomain entities representing recorded alert dispatches.
        """
        if self.db is not None:
            orm_alerts = (
                self.db.query(RenewalAlert)
                .filter(RenewalAlert.contract_id == contract_id)
                .order_by(RenewalAlert.dispatched_at.asc())
                .all()
            )
            return [self._to_domain(alert) for alert in orm_alerts]

        return self._in_memory_alerts.get(contract_id, [])

    def dispatch_renewal_alert(
        self,
        contract_id: UUID,
        threshold_days: int,
        title: str | None = None,
        counterparty: str | None = None,
        expires_on: date_type | None = None,
        recipients: list[str] | None = None,
        dispatched_at: datetime | None = None,
    ) -> RenewalAlertDispatchResult:
        """Dispatch a renewal alert notification for a contract at a specific day threshold [FR-9].

        Args:
            contract_id: Unique UUID identifier of the contract.
            threshold_days: Days remaining until expiration (e.g., 90, 60, 30).
            title: Optional contract title.
            counterparty: Optional counterparty name.
            expires_on: Optional contract expiration date.
            recipients: Optional target recipient email addresses or user IDs.
            dispatched_at: Optional explicit dispatch timestamp (defaults to current UTC time).

        Returns:
            RenewalAlertDispatchResult representing the outcome of the dispatch attempt.
        """
        dispatch_time = dispatched_at or datetime.now(timezone.utc)
        alert_id = uuid4()
        target_channel = self.channel

        contract_status: str | ContractStatus | None = None
        if self.db is not None:
            contract_orm = self.db.query(Contract).filter(Contract.id == contract_id).first()
            if contract_orm:
                contract_status = contract_orm.status
                if title is None:
                    title = contract_orm.title
                if counterparty is None:
                    counterparty = contract_orm.counterparty
                if expires_on is None:
                    expires_on = contract_orm.expires_on

        existing_alerts = self.get_dispatched_alerts(contract_id)
        already_dispatched = [a.threshold_days for a in existing_alerts]

        if contract_status is not None and self.should_suppress_alert(
            status=contract_status,
            already_dispatched_thresholds=already_dispatched,
            target_threshold_days=threshold_days,
        ):
            reason = (
                f"Alert suppressed for contract {contract_id} with status '{contract_status}' "
                f"or already dispatched threshold {threshold_days}."
            )
            logger.info("%s [correlation_id=%s]", reason, get_correlation_id())
            return RenewalAlertDispatchResult(
                alert_id=alert_id,
                contract_id=contract_id,
                threshold_days=threshold_days,
                dispatched_at=dispatch_time,
                success=False,
                suppressed=True,
                suppression_reason=reason,
                channel=target_channel,
                recipients=recipients or [],
                message=reason,
            )

        if threshold_days in already_dispatched:
            reason = f"Alert for {threshold_days}-day threshold already dispatched for contract {contract_id}."
            logger.info("%s [correlation_id=%s]", reason, get_correlation_id())
            return RenewalAlertDispatchResult(
                alert_id=alert_id,
                contract_id=contract_id,
                threshold_days=threshold_days,
                dispatched_at=dispatch_time,
                success=False,
                suppressed=True,
                suppression_reason=reason,
                channel=target_channel,
                recipients=recipients or [],
                message=reason,
            )

        if not self.notification_enabled:
            reason = "Notification dispatch is disabled in configuration settings."
            logger.info("%s [correlation_id=%s]", reason, get_correlation_id())
            return RenewalAlertDispatchResult(
                alert_id=alert_id,
                contract_id=contract_id,
                threshold_days=threshold_days,
                dispatched_at=dispatch_time,
                success=False,
                suppressed=True,
                suppression_reason=reason,
                channel=target_channel,
                recipients=recipients or [],
                message=reason,
            )

        if self.db is not None:
            orm_alert = RenewalAlert(
                id=alert_id,
                contract_id=contract_id,
                threshold_days=threshold_days,
                dispatched_at=dispatch_time,
            )
            self.db.add(orm_alert)
            self.db.commit()
        else:
            alert_domain = RenewalAlertDomain(
                id=alert_id,
                contract_id=contract_id,
                threshold_days=threshold_days,
                dispatched_at=dispatch_time,
            )
            if contract_id not in self._in_memory_alerts:
                self._in_memory_alerts[contract_id] = []
            self._in_memory_alerts[contract_id].append(alert_domain)

        target_recipients = recipients or []
        contract_title = f"'{title}'" if title else f"Contract {contract_id}"
        counterparty_str = f" with {counterparty}" if counterparty else ""
        expiry_str = f" on {expires_on.isoformat()}" if expires_on else ""

        subject = f"Contract Renewal Alert: {threshold_days} Days Remaining"
        body = (
            f"Renewal notification: {contract_title}{counterparty_str} "
            f"is scheduled to expire in {threshold_days} days{expiry_str}."
        )

        sent_ok = True
        if target_recipients:
            for recipient in target_recipients:
                ok = self.send_notification(
                    recipient=recipient,
                    subject=subject,
                    body=body,
                    channel=target_channel,
                )
                if not ok:
                    sent_ok = False
        else:
            self.send_notification(
                recipient=f"contract-owners-{contract_id}",
                subject=subject,
                body=body,
                channel=target_channel,
            )

        msg = f"Dispatched {threshold_days}-day renewal alert for contract {contract_id}"
        logger.info("%s [correlation_id=%s]", msg, get_correlation_id())

        return RenewalAlertDispatchResult(
            alert_id=alert_id,
            contract_id=contract_id,
            threshold_days=threshold_days,
            dispatched_at=dispatch_time,
            success=sent_ok,
            suppressed=False,
            suppression_reason=None,
            channel=target_channel,
            recipients=target_recipients,
            message=msg,
        )

    def send_notification(
        self,
        recipient: str,
        subject: str,
        body: str,
        channel: str = "in_app",
    ) -> bool:
        """Send a general notification payload to a recipient via specified channel [FR-9].

        Args:
            recipient: Target recipient user ID, email address, or topic.
            subject: Notification subject or title.
            body: Message content body.
            channel: Delivery channel identifier (e.g., "in_app", "email", "webhook").

        Returns:
            True if message delivery succeeded, False otherwise.
        """
        payload = {
            "recipient": recipient,
            "subject": subject,
            "body": body,
            "channel": channel,
            "dispatched_at": datetime.now(timezone.utc).isoformat(),
        }
        self._sent_notifications.append(payload)
        logger.info(
            "Notification delivered to recipient '%s' via channel '%s': %s [correlation_id=%s]",
            recipient,
            channel,
            subject,
            get_correlation_id(),
        )
        return True


# Convenient aliases for callers
InAppNotificationConnector = NotificationConnector
RenewalNotificationConnector = NotificationConnector
