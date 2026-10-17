"""Unit tests for trigger evaluation engine, deduplication hashing, and role/urgency routing matrix [FR-6, FR-7, FR-8, FR-11]."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.alerting import (
    AlertSuppression,
    AlertTrigger,
    AlertUrgency,
    NotificationChannel,
    NotificationPreference,
)
from src.models.database import Base
from src.services.alerting.evaluator import (
    FinancialEvent,
    TriggerEvaluator,
    compute_dedup_hash,
    evaluate_financial_event,
    evaluate_trigger_rule,
    get_channel_matrix,
    is_alert_suppressed,
    register_suppression,
    route_notification_channels,
)


@pytest.fixture
def db_session() -> Session:
    """Create an in-memory SQLite database session for unit testing."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def test_compute_dedup_hash_deterministic():
    rule_name = "PastDueThresholdRule"
    criteria = "past_due_invoices"
    entity_id = "inv-123"
    role_name = "Admin"
    urgency = AlertUrgency.HIGH.value

    hash1 = compute_dedup_hash(
        rule_name=rule_name,
        financial_criteria=criteria,
        entity_id=entity_id,
        role_name=role_name,
        urgency=urgency,
    )
    hash2 = compute_dedup_hash(
        rule_name=rule_name,
        financial_criteria=criteria,
        entity_id=entity_id,
        role_name=role_name,
        urgency=urgency,
    )

    assert isinstance(hash1, str)
    assert len(hash1) == 64
    assert hash1 == hash2


def test_compute_dedup_hash_different_inputs():
    hash1 = compute_dedup_hash(
        rule_name="RuleA",
        financial_criteria="past_due_invoices",
        entity_id="inv-123",
    )
    hash2 = compute_dedup_hash(
        rule_name="RuleA",
        financial_criteria="past_due_invoices",
        entity_id="inv-456",
    )

    assert hash1 != hash2


def test_suppression_lifecycle(db_session: Session):
    dedup_hash = compute_dedup_hash("Rule1", "past_due_invoices", "inv-1")

    # Initially not suppressed
    assert is_alert_suppressed(db_session, dedup_hash) is False

    # Register suppression for 10 seconds
    now = datetime.now(timezone.utc)
    suppression = register_suppression(db_session, dedup_hash, window_seconds=10, now=now)
    db_session.flush()

    assert suppression.dedup_hash == dedup_hash
    assert is_alert_suppressed(db_session, dedup_hash, now=now) is True

    # Check after window expiration (15 seconds later)
    later = now + timedelta(seconds=15)
    assert is_alert_suppressed(db_session, dedup_hash, now=later) is False


def test_register_suppression_update_existing(db_session: Session):
    dedup_hash = "test_hash_12345"
    now1 = datetime.now(timezone.utc)
    reg1 = register_suppression(db_session, dedup_hash, window_seconds=100, now=now1)
    db_session.flush()

    now2 = now1 + timedelta(seconds=50)
    reg2 = register_suppression(db_session, dedup_hash, window_seconds=200, now=now2)
    db_session.flush()

    assert reg1.suppression_id == reg2.suppression_id
    assert reg2.last_triggered_at == now2
    assert reg2.expires_at == now2 + timedelta(seconds=200)


def test_get_channel_matrix():
    low_channels = get_channel_matrix(AlertUrgency.LOW)
    assert low_channels == [NotificationChannel.IN_APP.value]

    med_channels = get_channel_matrix(AlertUrgency.MEDIUM)
    assert NotificationChannel.IN_APP.value in med_channels
    assert NotificationChannel.EMAIL.value in med_channels
    assert NotificationChannel.SMS.value not in med_channels

    high_channels = get_channel_matrix(AlertUrgency.HIGH)
    assert NotificationChannel.IN_APP.value in high_channels
    assert NotificationChannel.EMAIL.value in high_channels
    assert NotificationChannel.SMS.value in high_channels


def test_route_notification_channels_with_db_preference(db_session: Session):
    role_name = "FinancialAnalyst"
    event_type = "past_due_invoices"
    urgency = AlertUrgency.HIGH.value

    # Create user preference overriding channels
    pref = NotificationPreference(
        role_name=role_name,
        event_type=event_type,
        urgency=urgency,
        channels_json=["InApp", "Email"],  # Excludes SMS even for HIGH
    )
    db_session.add(pref)
    db_session.flush()

    channels = route_notification_channels(
        db=db_session,
        urgency=urgency,
        role_name=role_name,
        event_type=event_type,
    )

    assert channels == ["InApp", "Email"]


def test_route_notification_channels_fallback_to_matrix(db_session: Session):
    channels = route_notification_channels(
        db=db_session,
        urgency=AlertUrgency.LOW.value,
        role_name="UnconfiguredRole",
        event_type="unknown_event",
    )

    assert channels == [NotificationChannel.IN_APP.value]


def test_evaluate_trigger_rule():
    trigger = AlertTrigger(
        rule_name="LargePastDue",
        financial_criteria="past_due_invoices",
        threshold_amount=1000.0,
        urgency=AlertUrgency.HIGH.value,
        is_active=True,
    )

    assert evaluate_trigger_rule(trigger, amount=1500.0, criteria="past_due_invoices") is True
    assert evaluate_trigger_rule(trigger, amount=1000.0, criteria="past_due_invoices") is True
    assert evaluate_trigger_rule(trigger, amount=500.0, criteria="past_due_invoices") is False
    assert evaluate_trigger_rule(trigger, amount=1500.0, criteria="other_criteria") is False

    trigger.is_active = False
    assert evaluate_trigger_rule(trigger, amount=1500.0, criteria="past_due_invoices") is False


def test_evaluate_financial_event_e2e(db_session: Session):
    # Add active trigger rules
    trigger1 = AlertTrigger(
        rule_name="PastDueInvoiceOver500",
        financial_criteria="past_due_invoices",
        threshold_amount=500.0,
        urgency=AlertUrgency.MEDIUM.value,
        is_active=True,
    )
    trigger2 = AlertTrigger(
        rule_name="PastDueInvoiceOver5000",
        financial_criteria="past_due_invoices",
        threshold_amount=5000.0,
        urgency=AlertUrgency.HIGH.value,
        is_active=True,
    )
    db_session.add_all([trigger1, trigger2])
    db_session.flush()

    event = FinancialEvent(
        financial_criteria="past_due_invoices",
        amount=1200.0,
        entity_id="inv-100",
        role_name="Accountant",
    )

    # First evaluation
    results = evaluate_financial_event(db_session, event, window_seconds=600)
    assert len(results) == 1
    res1 = results[0]
    assert res1.triggered is True
    assert res1.rule_name == "PastDueInvoiceOver500"
    assert res1.urgency == AlertUrgency.MEDIUM.value
    assert res1.suppressed is False
    assert NotificationChannel.SMS.value not in res1.channels  # Medium urgency does not send SMS

    # Second evaluation within suppression window
    results_suppressed = evaluate_financial_event(db_session, event, window_seconds=600)
    assert len(results_suppressed) == 1
    res2 = results_suppressed[0]
    assert res2.triggered is True
    assert res2.suppressed is True


def test_trigger_evaluator_class(db_session: Session):
    evaluator = TriggerEvaluator(db=db_session)

    trigger = AlertTrigger(
        rule_name="ClassTestRule",
        financial_criteria="quote_expired",
        threshold_amount=100.0,
        urgency=AlertUrgency.LOW.value,
        is_active=True,
    )
    db_session.add(trigger)
    db_session.flush()

    event = FinancialEvent(
        financial_criteria="quote_expired",
        amount=250.0,
        entity_id="quote-1",
    )

    results = evaluator.evaluate_event(event)
    assert len(results) == 1
    assert results[0].rule_name == "ClassTestRule"
    assert results[0].urgency == AlertUrgency.LOW.value
    assert results[0].channels == [NotificationChannel.IN_APP.value]
