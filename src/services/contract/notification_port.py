"""Abstract renewal alert notification dispatch interface for contract lifecycle management [FR-9].

This module defines the abstract port contract and execution DTOs for dispatching contract
expiration and renewal alert notifications at 90, 60, and 30-day thresholds [FR-9].
Decouples core business logic and workers from external notification connectors (e.g., in-app alerts, email, webhooks).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date as date_type, datetime, timezone
from uuid import UUID

from src.services.contract.models import ContractStatus, RenewalAlertDomain

# Standard renewal alert day thresholds prior to contract expiration [FR-9]
RENEWAL_ALERT_THRESHOLDS: tuple[int, ...] = (90, 60, 30)

# Contract statuses for which renewal alerts must be suppressed [FR-9]
SUPPRESSED_CONTRACT_STATUSES: set[ContractStatus | str] = {
    ContractStatus.TERMINATED,
    ContractStatus.EXPIRED,
    ContractStatus.DRAFT,
}


@dataclass
class RenewalAlertDispatchResult:
    """Standardized DTO representing the outcome of a renewal alert dispatch attempt [FR-9]."""

    alert_id: UUID
    contract_id: UUID
    threshold_days: int
    dispatched_at: datetime
    success: bool = True
    suppressed: bool = False
    suppression_reason: str | None = None
    channel: str = "in_app"
    recipients: list[str] = field(default_factory=list)
    message: str = "Renewal alert processed"


class NotificationPort(ABC):
    """Abstract port contract for contract renewal alert dispatch and notification delivery [FR-9]."""

    @abstractmethod
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
            title: Optional contract title for notification formatting.
            counterparty: Optional counterparty name.
            expires_on: Optional contract expiration date.
            recipients: Optional target recipient email addresses or user IDs.
            dispatched_at: Optional explicit dispatch timestamp (defaults to current UTC time).

        Returns:
            RenewalAlertDispatchResult representing the outcome of the dispatch attempt.
        """
        ...

    @abstractmethod
    def should_suppress_alert(
        self,
        status: ContractStatus | str,
        already_dispatched_thresholds: list[int] | None = None,
        target_threshold_days: int | None = None,
    ) -> bool:
        """Evaluate whether a renewal alert should be suppressed for a contract [FR-9].

        Alerts are suppressed if the contract is terminated, expired, draft, or if an alert
        for the given threshold_days has already been dispatched.

        Args:
            status: Current contract lifecycle status.
            already_dispatched_thresholds: Optional list of thresholds already dispatched.
            target_threshold_days: Optional specific threshold days being evaluated.

        Returns:
            True if alert should be suppressed, False otherwise.
        """
        ...

    @abstractmethod
    def calculate_days_until_expiration(
        self,
        expires_on: date_type,
        reference_date: date_type | None = None,
    ) -> int:
        """Calculate the remaining days from a reference date until the contract expiration date [FR-9].

        Args:
            expires_on: Contract expiration date.
            reference_date: Optional reference date (defaults to current UTC date).

        Returns:
            Integer representing remaining days until expiration.
        """
        ...

    @abstractmethod
    def get_dispatched_alerts(self, contract_id: UUID) -> list[RenewalAlertDomain]:
        """Retrieve all previously dispatched renewal alerts for a contract [FR-9].

        Args:
            contract_id: Unique UUID identifier of the contract.

        Returns:
            List of RenewalAlertDomain entities representing recorded alert dispatches.
        """
        ...

    @abstractmethod
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
            True if message was delivered successfully, False otherwise.
        """
        ...
