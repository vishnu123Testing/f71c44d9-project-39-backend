"""Trigger evaluation engine, alert deduplication hashing, and role/urgency channel matrix routing [FR-6, FR-7, FR-8, FR-11].

This module implements core domain logic for:
1. Evaluating financial state updates/events against configured AlertTrigger rules [FR-6].
2. Computing deterministic SHA-256 deduplication hashes and managing alert suppressions within a configurable window [FR-11].
3. Routing notification dispatches across In-App, Email, and SMS channels using a role/urgency routing matrix [FR-7, FR-8].
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.lib.config import get_settings
from src.models.alerting import (
    AlertSuppression,
    AlertTrigger,
    AlertUrgency,
    NotificationChannel,
    NotificationPreference,
)


@dataclass
class FinancialEvent:
    """DTO representing an incoming financial state update or event [FR-6]."""

    financial_criteria: str
    amount: float
    entity_id: str | uuid.UUID | None = None
    event_type: str | None = None
    role_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvaluationResult:
    """DTO representing the outcome of evaluating a trigger rule against a financial event [FR-6, FR-11]."""

    triggered: bool
    trigger_id: uuid.UUID | None = None
    rule_name: str | None = None
    financial_criteria: str | None = None
    threshold_amount: float | None = None
    urgency: str = AlertUrgency.MEDIUM.value
    suppressed: bool = False
    dedup_hash: str | None = None
    channels: list[str] = field(default_factory=list)
    reason: str | None = None


def compute_dedup_hash(
    rule_name: str,
    financial_criteria: str,
    entity_id: str | uuid.UUID | None = None,
    role_name: str | None = None,
    urgency: str | None = None,
) -> str:
    """Compute a deterministic SHA-256 deduplication hash for an alert instance [FR-11].

    Args:
        rule_name: The name of the trigger rule.
        financial_criteria: The financial criteria key (e.g. 'past_due_invoices_exceed_threshold').
        entity_id: Optional ID of the associated entity (e.g. invoice_id, customer_id).
        role_name: Optional recipient role name.
        urgency: Optional urgency string.

    Returns:
        A 64-character hex SHA-256 hash string.
    """
    entity_str = str(entity_id) if entity_id is not None else ""
    role_str = str(role_name) if role_name is not None else ""
    urgency_str = str(urgency) if urgency is not None else ""

    raw_key = f"rule:{rule_name}|criteria:{financial_criteria}|entity:{entity_str}|role:{role_str}|urgency:{urgency_str}"
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def is_alert_suppressed(
    db: Session,
    dedup_hash: str,
    now: datetime | None = None,
) -> bool:
    """Check if an alert matching the deduplication hash is currently suppressed [FR-11].

    Args:
        db: Active synchronous SQLAlchemy database session.
        dedup_hash: SHA-256 deduplication hash string.
        now: Optional reference UTC datetime. Defaults to current UTC time.

    Returns:
        True if an active, non-expired suppression record exists; False otherwise.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    stmt = select(AlertSuppression).where(AlertSuppression.dedup_hash == dedup_hash)
    suppression = db.scalars(stmt).first()

    if suppression is not None and suppression.expires_at > now:
        return True
    return False


def register_suppression(
    db: Session,
    dedup_hash: str,
    window_seconds: int | None = None,
    now: datetime | None = None,
) -> AlertSuppression:
    """Create or update a suppression record for a deduplication hash [FR-11].

    Args:
        db: Active synchronous SQLAlchemy database session.
        dedup_hash: SHA-256 deduplication hash string.
        window_seconds: Duration of the suppression window in seconds.
        now: Optional reference UTC datetime. Defaults to current UTC time.

    Returns:
        The updated or created AlertSuppression ORM object.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if window_seconds is None:
        cfg = get_settings()
        window_seconds = getattr(cfg, "ALERT_SUPPRESSION_WINDOW_SECONDS", 900)

    expires_at = now + timedelta(seconds=window_seconds)

    stmt = select(AlertSuppression).where(AlertSuppression.dedup_hash == dedup_hash)
    existing = db.scalars(stmt).first()

    if existing is not None:
        existing.last_triggered_at = now
        existing.expires_at = expires_at
        return existing

    suppression = AlertSuppression(
        dedup_hash=dedup_hash,
        last_triggered_at=now,
        expires_at=expires_at,
    )
    db.add(suppression)
    return suppression


def get_channel_matrix(urgency: str | AlertUrgency) -> list[str]:
    """Return default notification channels mapped to urgency levels [FR-8].

    Default mapping:
    - Low urgency: InApp only.
    - Medium urgency: InApp and Email. (No SMS for Low/Medium per Scenario 2).
    - High urgency: InApp, Email, and SMS.

    Args:
        urgency: Urgency string or AlertUrgency enum.

    Returns:
        List of notification channel string values (e.g. ['InApp', 'Email']).
    """
    urgency_val = urgency.value if isinstance(urgency, AlertUrgency) else str(urgency)

    if urgency_val == AlertUrgency.LOW.value:
        return [NotificationChannel.IN_APP.value]
    elif urgency_val == AlertUrgency.MEDIUM.value:
        return [NotificationChannel.IN_APP.value, NotificationChannel.EMAIL.value]
    elif urgency_val == AlertUrgency.HIGH.value:
        return [
            NotificationChannel.IN_APP.value,
            NotificationChannel.EMAIL.value,
            NotificationChannel.SMS.value,
        ]

    return [NotificationChannel.IN_APP.value]


def route_notification_channels(
    db: Session | None,
    urgency: str | AlertUrgency,
    role_name: str | None = None,
    event_type: str | None = None,
) -> list[str]:
    """Determine notification channels based on role preferences and urgency matrix [FR-7, FR-8].

    Checks user notification preferences for the role and event type. If configured, uses
    those preferences; otherwise falls back to the default urgency channel matrix.

    Args:
        db: Optional database session to query user preferences.
        urgency: Urgency level ('Low', 'Medium', 'High').
        role_name: Optional user role name.
        event_type: Optional event type string.

    Returns:
        List of channel string identifiers (e.g., ['InApp', 'Email']).
    """
    urgency_val = urgency.value if isinstance(urgency, AlertUrgency) else str(urgency)

    if db is not None and role_name:
        # Check specific preference by role_name and event_type
        if event_type:
            stmt = select(NotificationPreference).where(
                NotificationPreference.role_name == role_name,
                NotificationPreference.event_type == event_type,
                NotificationPreference.urgency == urgency_val,
            )
            pref = db.scalars(stmt).first()

            if pref is None:
                # Try generic event_type match
                stmt = select(NotificationPreference).where(
                    NotificationPreference.role_name == role_name,
                    NotificationPreference.event_type == "all",
                    NotificationPreference.urgency == urgency_val,
                )
                pref = db.scalars(stmt).first()

            if pref is not None and pref.channels_json:
                channels = pref.channels_json
                if isinstance(channels, list) and len(channels) > 0:
                    return [str(c) for c in channels]
                elif isinstance(channels, dict) and "channels" in channels:
                    c_list = channels.get("channels")
                    if isinstance(c_list, list) and len(c_list) > 0:
                        return [str(c) for c in c_list]

    return get_channel_matrix(urgency_val)


def evaluate_trigger_rule(
    trigger: AlertTrigger,
    amount: float,
    criteria: str | None = None,
) -> bool:
    """Evaluate whether a single trigger rule matches financial threshold criteria [FR-6].

    Args:
        trigger: The AlertTrigger rule ORM object.
        amount: The financial amount to evaluate against threshold_amount.
        criteria: Optional financial criteria string to match against trigger criteria.

    Returns:
        True if the rule is active, criteria matches, and amount >= threshold_amount.
    """
    if not trigger.is_active:
        return False

    if criteria is not None and trigger.financial_criteria != criteria:
        return False

    return float(amount) >= float(trigger.threshold_amount)


def evaluate_financial_event(
    db: Session,
    event: FinancialEvent,
    window_seconds: int | None = None,
    auto_flush: bool = True,
) -> list[EvaluationResult]:
    """Evaluate an incoming financial event against all active trigger rules in the database [FR-6, FR-11].

    Args:
        db: Active synchronous SQLAlchemy database session.
        event: Incoming FinancialEvent DTO.
        window_seconds: Optional custom suppression window duration in seconds.
        auto_flush: Whether to flush session after registering suppressions.

    Returns:
        List of EvaluationResult DTOs for each matching trigger rule.
    """
    stmt = select(AlertTrigger).where(
        AlertTrigger.is_active.is_(True),
        AlertTrigger.financial_criteria == event.financial_criteria,
    )
    triggers = list(db.scalars(stmt).all())

    results: list[EvaluationResult] = []

    for trigger in triggers:
        if evaluate_trigger_rule(trigger, event.amount, event.financial_criteria):
            dedup_hash = compute_dedup_hash(
                rule_name=trigger.rule_name,
                financial_criteria=trigger.financial_criteria,
                entity_id=event.entity_id,
                role_name=event.role_name,
                urgency=trigger.urgency,
            )

            suppressed = is_alert_suppressed(db, dedup_hash)
            channels = route_notification_channels(
                db=db,
                urgency=trigger.urgency,
                role_name=event.role_name,
                event_type=event.event_type or event.financial_criteria,
            )

            if not suppressed:
                register_suppression(db, dedup_hash, window_seconds=window_seconds)
                if auto_flush:
                    db.flush()

            results.append(
                EvaluationResult(
                    triggered=True,
                    trigger_id=trigger.trigger_id,
                    rule_name=trigger.rule_name,
                    financial_criteria=trigger.financial_criteria,
                    threshold_amount=float(trigger.threshold_amount),
                    urgency=trigger.urgency,
                    suppressed=suppressed,
                    dedup_hash=dedup_hash,
                    channels=channels,
                    reason=f"Financial amount {event.amount} met or exceeded threshold {trigger.threshold_amount}",
                )
            )

    return results


class TriggerEvaluator:
    """Service wrapper for evaluating financial alert triggers, deduplication, and channel routing [FR-6, FR-7, FR-8, FR-11]."""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def evaluate_event(
        self,
        event: FinancialEvent,
        db: Session | None = None,
        window_seconds: int | None = None,
    ) -> list[EvaluationResult]:
        """Evaluate a financial event against active trigger rules [FR-6]."""
        active_db = db or self.db
        if active_db is None:
            raise ValueError("Database session is required to evaluate financial events")
        return evaluate_financial_event(active_db, event, window_seconds=window_seconds)

    def evaluate_rule(
        self,
        trigger: AlertTrigger,
        amount: float,
        criteria: str | None = None,
    ) -> bool:
        """Evaluate whether a single trigger rule matches [FR-6]."""
        return evaluate_trigger_rule(trigger, amount, criteria)

    def compute_hash(
        self,
        rule_name: str,
        financial_criteria: str,
        entity_id: str | uuid.UUID | None = None,
        role_name: str | None = None,
        urgency: str | None = None,
    ) -> str:
        """Compute a deterministic SHA-256 deduplication hash [FR-11]."""
        return compute_dedup_hash(
            rule_name=rule_name,
            financial_criteria=financial_criteria,
            entity_id=entity_id,
            role_name=role_name,
            urgency=urgency,
        )

    def is_suppressed(
        self,
        dedup_hash: str,
        db: Session | None = None,
    ) -> bool:
        """Check if an alert is currently suppressed [FR-11]."""
        active_db = db or self.db
        if active_db is None:
            raise ValueError("Database session is required to check suppression status")
        return is_alert_suppressed(active_db, dedup_hash)

    def register_suppression(
        self,
        dedup_hash: str,
        db: Session | None = None,
        window_seconds: int | None = None,
    ) -> AlertSuppression:
        """Register or extend a suppression entry [FR-11]."""
        active_db = db or self.db
        if active_db is None:
            raise ValueError("Database session is required to register suppression")
        return register_suppression(active_db, dedup_hash, window_seconds=window_seconds)

    def route_channels(
        self,
        urgency: str | AlertUrgency,
        role_name: str | None = None,
        event_type: str | None = None,
        db: Session | None = None,
    ) -> list[str]:
        """Determine notification delivery channels [FR-7, FR-8]."""
        active_db = db or self.db
        return route_notification_channels(
            db=active_db,
            urgency=urgency,
            role_name=role_name,
            event_type=event_type,
        )
