"""Unit tests for the NotificationPort abstract interface, DTOs, and constants [FR-9]."""

from datetime import date as date_type, datetime, timezone
import uuid

import pytest

from src.services.contract.models import ContractStatus, RenewalAlertDomain
from src.services.contract.notification_port import (
    RENEWAL_ALERT_THRESHOLDS,
    SUPPRESSED_CONTRACT_STATUSES,
    NotificationPort,
    RenewalAlertDispatchResult,
)


class DummyNotificationConnector(NotificationPort):
    """Concrete test implementation of NotificationPort for unit testing [FR-9]."""

    def __init__(self) -> None:
        self.dispatched_alerts: dict[uuid.UUID, list[RenewalAlertDomain]] = {}
        self.sent_notifications: list[dict[str, str]] = []

    def dispatch_renewal_alert(
        self,
        contract_id: uuid.UUID,
        threshold_days: int,
        title: str | None = None,
        counterparty: str | None = None,
        expires_on: date_type | None = None,
        recipients: list[str] | None = None,
        dispatched_at: datetime | None = None,
    ) -> RenewalAlertDispatchResult:
        dispatch_time = dispatched_at or datetime.now(timezone.utc)
        alert_id = uuid.uuid4()

        # Check suppression rules
        # In a real connector, status is checked via database or aggregate
        alert_domain = RenewalAlertDomain(
            id=alert_id,
            contract_id=contract_id,
            threshold_days=threshold_days,
            dispatched_at=dispatch_time,
        )
        if contract_id not in self.dispatched_alerts:
            self.dispatched_alerts[contract_id] = []
        self.dispatched_alerts[contract_id].append(alert_domain)

        return RenewalAlertDispatchResult(
            alert_id=alert_id,
            contract_id=contract_id,
            threshold_days=threshold_days,
            dispatched_at=dispatch_time,
            success=True,
            suppressed=False,
            channel="in_app",
            recipients=recipients or [],
            message=f"Dispatched {threshold_days}-day renewal alert for contract {contract_id}",
        )

    def should_suppress_alert(
        self,
        status: ContractStatus | str,
        already_dispatched_thresholds: list[int] | None = None,
        target_threshold_days: int | None = None,
    ) -> bool:
        status_val = status.value if isinstance(status, ContractStatus) else status
        if status_val in {s.value if isinstance(s, ContractStatus) else s for s in SUPPRESSED_CONTRACT_STATUSES}:
            return True
        if (
            already_dispatched_thresholds is not None
            and target_threshold_days is not None
            and target_threshold_days in already_dispatched_thresholds
        ):
            return True
        return False

    def calculate_days_until_expiration(
        self,
        expires_on: date_type,
        reference_date: date_type | None = None,
    ) -> int:
        ref = reference_date or datetime.now(timezone.utc).date()
        return (expires_on - ref).days

    def get_dispatched_alerts(self, contract_id: uuid.UUID) -> list[RenewalAlertDomain]:
        return self.dispatched_alerts.get(contract_id, [])

    def send_notification(
        self,
        recipient: str,
        subject: str,
        body: str,
        channel: str = "in_app",
    ) -> bool:
        self.sent_notifications.append(
            {"recipient": recipient, "subject": subject, "body": body, "channel": channel}
        )
        return True


def test_cannot_instantiate_abstract_notification_port() -> None:
    """Verify that NotificationPort cannot be instantiated directly without implementing abstract methods."""
    with pytest.raises(TypeError):
        NotificationPort()  # type: ignore[abstract]


def test_renewal_alert_constants() -> None:
    """Verify renewal alert thresholds and suppressed contract statuses [FR-9]."""
    assert RENEWAL_ALERT_THRESHOLDS == (90, 60, 30)
    assert ContractStatus.TERMINATED in SUPPRESSED_CONTRACT_STATUSES
    assert ContractStatus.EXPIRED in SUPPRESSED_CONTRACT_STATUSES
    assert ContractStatus.DRAFT in SUPPRESSED_CONTRACT_STATUSES


def test_renewal_alert_dispatch_result_dto() -> None:
    """Verify RenewalAlertDispatchResult DTO instantiation and attributes."""
    alert_id = uuid.uuid4()
    contract_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    result = RenewalAlertDispatchResult(
        alert_id=alert_id,
        contract_id=contract_id,
        threshold_days=90,
        dispatched_at=now,
        success=True,
        suppressed=False,
        channel="email",
        recipients=["admin@example.com"],
        message="Alert sent",
    )

    assert result.alert_id == alert_id
    assert result.contract_id == contract_id
    assert result.threshold_days == 90
    assert result.dispatched_at == now
    assert result.success is True
    assert result.suppressed is False
    assert result.channel == "email"
    assert result.recipients == ["admin@example.com"]
    assert result.message == "Alert sent"


def test_dummy_notification_connector_dispatch_and_suppression() -> None:
    """Verify notification connector dispatch, suppression logic, and expiration calculation [FR-9]."""
    connector = DummyNotificationConnector()
    contract_id = uuid.uuid4()

    # Test expiration date calculation
    ref_date = date_type(2026, 1, 1)
    exp_date = date_type(2026, 4, 1)  # 90 days later
    days_left = connector.calculate_days_until_expiration(expires_on=exp_date, reference_date=ref_date)
    assert days_left == 90

    # Test alert suppression evaluation
    assert connector.should_suppress_alert(ContractStatus.TERMINATED) is True
    assert connector.should_suppress_alert(ContractStatus.EXPIRED) is True
    assert connector.should_suppress_alert(ContractStatus.ACTIVE) is False

    # Suppress if threshold already dispatched
    assert connector.should_suppress_alert(
        status=ContractStatus.ACTIVE,
        already_dispatched_thresholds=[90],
        target_threshold_days=90,
    ) is True
    assert connector.should_suppress_alert(
        status=ContractStatus.ACTIVE,
        already_dispatched_thresholds=[90],
        target_threshold_days=60,
    ) is False

    # Test dispatching renewal alert
    result = connector.dispatch_renewal_alert(
        contract_id=contract_id,
        threshold_days=90,
        title="Master Services Agreement",
        counterparty="Acme Corp",
        expires_on=exp_date,
        recipients=["legal@acme.com"],
    )

    assert result.success is True
    assert result.threshold_days == 90
    assert result.contract_id == contract_id

    # Verify dispatched alerts history query
    alerts = connector.get_dispatched_alerts(contract_id)
    assert len(alerts) == 1
    assert alerts[0].threshold_days == 90

    # Test general notification send
    send_ok = connector.send_notification(
        recipient="user1@example.com",
        subject="Renewal Notice",
        body="Contract expires in 90 days",
        channel="in_app",
    )
    assert send_ok is True
    assert len(connector.sent_notifications) == 1
    assert connector.sent_notifications[0]["subject"] == "Renewal Notice"
