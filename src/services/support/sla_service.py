"""SLA Service implementation for support ticket management operations [FR-4, FR-5, FR-6, NFR-3, NFR-4].

This module encapsulates core domain business rules for calculating SLA target deadlines,
initializing SLA tracking records, detecting impending breach warnings, flagging SLA breaches,
recalculating target deadlines upon ticket priority changes, and executing automated escalation pathways.
"""

from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.lib.config import settings
from src.lib.logging import get_correlation_id
from src.models.ticket import SLATracking, SupportTicket, TicketEscalation

logger = logging.getLogger(__name__)


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Ensure a datetime instance is timezone-aware in UTC.

    Args:
        dt: Optional datetime object.

    Returns:
        Timezone-aware datetime in UTC, or None if input was None.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@runtime_checkable
class NotificationPortProtocol(Protocol):
    """Protocol defining expected notification port interface for SLA alerts [NFR-4]."""

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
    ) -> Any: ...

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
    ) -> Any: ...

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
    ) -> Any: ...


def calculate_target_deadlines(
    priority: str,
    created_at: datetime | None = None,
) -> dict[str, datetime]:
    """Calculate target due datetimes for first_response and resolution SLA targets [FR-4].

    Args:
        priority: Ticket priority string ("P1", "P2", "P3").
        created_at: Base creation timestamp (defaults to current UTC time if None).

    Returns:
        Dict mapping target_type ("first_response", "resolution") to target due UTC datetime.
    """
    base_time = _ensure_utc(created_at) or datetime.now(timezone.utc)
    norm_priority = priority.strip().upper() if priority else "P3"

    if norm_priority == "P1":
        resp_hours = getattr(settings, "SLA_P1_RESPONSE_HOURS", 1)
        res_hours = getattr(settings, "SLA_P1_RESOLUTION_HOURS", 4)
    elif norm_priority == "P2":
        resp_hours = getattr(settings, "SLA_P2_RESPONSE_HOURS", 4)
        res_hours = getattr(settings, "SLA_P2_RESOLUTION_HOURS", 24)
    elif norm_priority == "P3":
        resp_hours = getattr(settings, "SLA_P3_RESPONSE_HOURS", 24)
        res_hours = getattr(settings, "SLA_P3_RESOLUTION_HOURS", 72)
    else:
        default_hours = getattr(settings, "DEFAULT_SLA_THRESHOLD_HOURS", 24)
        resp_hours = default_hours
        res_hours = default_hours

    return {
        "first_response": base_time + timedelta(hours=resp_hours),
        "resolution": base_time + timedelta(hours=res_hours),
    }


def initialize_sla_tracking(
    session: Session,
    ticket: SupportTicket,
) -> list[SLATracking]:
    """Initialize SLA tracking records for a newly created support ticket [FR-4].

    Args:
        session: Active synchronous database session.
        ticket: SupportTicket ORM model instance.

    Returns:
        List of initialized SLATracking ORM instances.
    """
    if ticket.sla_trackings and len(ticket.sla_trackings) > 0:
        return ticket.sla_trackings

    deadlines = calculate_target_deadlines(
        priority=ticket.priority,
        created_at=ticket.created_at,
    )

    trackings: list[SLATracking] = []
    for target_type, target_due_at in deadlines.items():
        tracking = SLATracking(
            ticket_id=ticket.id,
            target_type=target_type,
            target_due_at=target_due_at,
            warning_sent_at=None,
            breached_at=None,
            is_breached=False,
        )
        session.add(tracking)
        trackings.append(tracking)

    logger.info(
        json.dumps(
            {
                "event": "sla_tracking_initialized",
                "ticket_id": str(ticket.id),
                "client_id": ticket.client_id,
                "priority": ticket.priority,
                "first_response_due": deadlines["first_response"].isoformat(),
                "resolution_due": deadlines["resolution"].isoformat(),
                "correlation_id": get_correlation_id(),
            }
        )
    )

    return trackings


def recalculate_sla_targets(
    session: Session,
    ticket: SupportTicket,
    new_priority: str,
    now: datetime | None = None,
) -> list[SLATracking]:
    """Recalculate SLA target due datetimes and update status on priority change [FR-4].

    Args:
        session: Active synchronous database session.
        ticket: SupportTicket ORM model instance.
        new_priority: New target priority tier ("P1", "P2", "P3").
        now: Optional reference evaluation timestamp (defaults to current UTC time).

    Returns:
        Updated list of SLATracking ORM instances.
    """
    eval_now = _ensure_utc(now) or datetime.now(timezone.utc)
    old_priority = ticket.priority
    norm_new_priority = new_priority.strip().upper()
    ticket.priority = norm_new_priority

    deadlines = calculate_target_deadlines(
        priority=norm_new_priority,
        created_at=ticket.created_at,
    )

    if not ticket.sla_trackings:
        trackings = initialize_sla_tracking(session, ticket)
    else:
        trackings = ticket.sla_trackings

    for tracking in trackings:
        new_due_at = deadlines.get(tracking.target_type)
        if not new_due_at:
            continue

        tracking.target_due_at = new_due_at

        # Check completed status
        is_completed = False
        if tracking.target_type == "first_response" and ticket.first_responded_at is not None:
            is_completed = True
            responded_at = _ensure_utc(ticket.first_responded_at)
            tracking.is_breached = bool(responded_at and responded_at > new_due_at)
            if tracking.is_breached:
                tracking.breached_at = tracking.breached_at or new_due_at
            else:
                tracking.breached_at = None

        elif tracking.target_type == "resolution" and ticket.resolved_at is not None:
            is_completed = True
            resolved_at = _ensure_utc(ticket.resolved_at)
            tracking.is_breached = bool(resolved_at and resolved_at > new_due_at)
            if tracking.is_breached:
                tracking.breached_at = tracking.breached_at or new_due_at
            else:
                tracking.breached_at = None

        if not is_completed:
            if eval_now > new_due_at:
                tracking.is_breached = True
                tracking.breached_at = tracking.breached_at or eval_now
            else:
                tracking.is_breached = False
                tracking.breached_at = None

    logger.info(
        json.dumps(
            {
                "event": "sla_targets_recalculated",
                "ticket_id": str(ticket.id),
                "client_id": ticket.client_id,
                "old_priority": old_priority,
                "new_priority": norm_new_priority,
                "new_first_response_due": deadlines["first_response"].isoformat(),
                "new_resolution_due": deadlines["resolution"].isoformat(),
                "correlation_id": get_correlation_id(),
            }
        )
    )

    return trackings


def _get_next_tier(current_tier: str) -> str | None:
    """Determine the next escalation support tier.

    Args:
        current_tier: Current tier string (e.g., "Tier 1", "Tier 2").

    Returns:
        Next tier string, or None if already at maximum tier.
    """
    tier_lower = current_tier.strip().lower()
    if "tier 1" in tier_lower:
        return "Tier 2"
    if "tier 2" in tier_lower:
        return "Tier 3"
    return None


def evaluate_ticket_sla(
    session: Session,
    ticket: SupportTicket,
    notification_port: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate active SLA countdown timers, detect breach warnings, flag breaches, and execute escalations [FR-4, FR-5, FR-6].

    Args:
        session: Active synchronous database session.
        ticket: SupportTicket ORM model instance.
        notification_port: Optional notification port adapter instance.
        now: Reference evaluation timestamp (defaults to current UTC time).

    Returns:
        Dict summarizing evaluation results for the ticket.
    """
    eval_now = _ensure_utc(now) or datetime.now(timezone.utc)
    ticket_created_at = _ensure_utc(ticket.created_at) or eval_now

    result: dict[str, Any] = {
        "ticket_id": str(ticket.id),
        "client_id": ticket.client_id,
        "priority": ticket.priority,
        "warnings_sent": [],
        "breaches_detected": [],
        "escalated": False,
        "source_tier": None,
        "target_tier": None,
    }

    if ticket.status in ("resolved", "closed"):
        return result

    if not ticket.sla_trackings:
        initialize_sla_tracking(session, ticket)

    warning_threshold_pct = getattr(settings, "SLA_WARNING_THRESHOLD_PERCENT", 80.0)
    escalation_threshold_pct = getattr(settings, "SLA_ESCALATION_THRESHOLD_PERCENT", 80.0)
    recipient_roles = getattr(settings, "ESCALATION_RECIPIENT_ROLES", ["SalesManager", "Admin"])

    resolution_elapsed_pct = 0.0
    resolution_is_breached = False

    for tracking in ticket.sla_trackings:
        target_due_at = _ensure_utc(tracking.target_due_at) or eval_now
        total_duration_sec = (target_due_at - ticket_created_at).total_seconds()
        elapsed_sec = (eval_now - ticket_created_at).total_seconds()

        if total_duration_sec > 0:
            elapsed_pct = (elapsed_sec / total_duration_sec) * 100.0
        else:
            elapsed_pct = 100.0

        remaining_minutes = max(0.0, (target_due_at - eval_now).total_seconds() / 60.0)

        is_completed = False
        if tracking.target_type == "first_response" and ticket.first_responded_at is not None:
            is_completed = True
        elif tracking.target_type == "resolution" and ticket.resolved_at is not None:
            is_completed = True

        if tracking.target_type == "resolution":
            resolution_elapsed_pct = elapsed_pct
            resolution_is_breached = tracking.is_breached

        if is_completed:
            continue

        # Check Breach Condition
        if eval_now > target_due_at:
            if not tracking.is_breached:
                tracking.is_breached = True
                tracking.breached_at = eval_now
                result["breaches_detected"].append(tracking.target_type)

                if tracking.target_type == "resolution":
                    resolution_is_breached = True

                logger.warning(
                    json.dumps(
                        {
                            "event": "sla_breach_detected",
                            "ticket_id": str(ticket.id),
                            "client_id": ticket.client_id,
                            "target_type": tracking.target_type,
                            "priority": ticket.priority,
                            "target_due_at": target_due_at.isoformat(),
                            "breached_at": eval_now.isoformat(),
                            "correlation_id": get_correlation_id(),
                        }
                    )
                )

                if notification_port and hasattr(notification_port, "send_breach_alert"):
                    try:
                        notification_port.send_breach_alert(
                            ticket_id=ticket.id,
                            client_id=ticket.client_id,
                            priority=ticket.priority,
                            target_type=tracking.target_type,
                            breached_at=eval_now,
                            recipient_roles=recipient_roles,
                        )
                    except Exception as exc:
                        logger.error(
                            f"Failed to dispatch breach alert for ticket {ticket.id}: {exc}"
                        )

        # Check Warning Condition
        elif elapsed_pct >= warning_threshold_pct:
            if tracking.warning_sent_at is None and not tracking.is_breached:
                tracking.warning_sent_at = eval_now
                result["warnings_sent"].append(tracking.target_type)

                logger.info(
                    json.dumps(
                        {
                            "event": "sla_warning_threshold_crossed",
                            "ticket_id": str(ticket.id),
                            "client_id": ticket.client_id,
                            "target_type": tracking.target_type,
                            "priority": ticket.priority,
                            "elapsed_percent": round(elapsed_pct, 2),
                            "remaining_minutes": round(remaining_minutes, 2),
                            "correlation_id": get_correlation_id(),
                        }
                    )
                )

                if notification_port and hasattr(notification_port, "send_breach_warning"):
                    try:
                        notification_port.send_breach_warning(
                            ticket_id=ticket.id,
                            client_id=ticket.client_id,
                            priority=ticket.priority,
                            target_type=tracking.target_type,
                            target_due_at=target_due_at,
                            remaining_minutes=remaining_minutes,
                            recipient_roles=recipient_roles,
                        )
                    except Exception as exc:
                        logger.error(
                            f"Failed to dispatch breach warning for ticket {ticket.id}: {exc}"
                        )

    # Escalation Pathway Evaluation [FR-6]
    if ticket.status in ("open", "pending"):
        if resolution_elapsed_pct >= escalation_threshold_pct or resolution_is_breached:
            next_tier = _get_next_tier(ticket.assigned_tier)
            if next_tier:
                # Check if ticket was already escalated to next_tier
                already_escalated = any(
                    esc.target_tier == next_tier for esc in ticket.escalations
                )
                if not already_escalated:
                    source_tier = ticket.assigned_tier
                    ticket.assigned_tier = next_tier
                    reason = (
                        f"Automated SLA escalation: Resolution SLA threshold "
                        f"({resolution_elapsed_pct:.1f}%) crossed without resolution"
                    )

                    escalation_record = TicketEscalation(
                        ticket_id=ticket.id,
                        source_tier=source_tier,
                        target_tier=next_tier,
                        escalated_at=eval_now,
                        reason=reason,
                    )
                    session.add(escalation_record)

                    result["escalated"] = True
                    result["source_tier"] = source_tier
                    result["target_tier"] = next_tier

                    logger.warning(
                        json.dumps(
                            {
                                "event": "ticket_escalated",
                                "ticket_id": str(ticket.id),
                                "client_id": ticket.client_id,
                                "priority": ticket.priority,
                                "source_tier": source_tier,
                                "target_tier": next_tier,
                                "reason": reason,
                                "correlation_id": get_correlation_id(),
                            }
                        )
                    )

                    if notification_port and hasattr(notification_port, "send_escalation_alert"):
                        try:
                            notification_port.send_escalation_alert(
                                ticket_id=ticket.id,
                                client_id=ticket.client_id,
                                priority=ticket.priority,
                                source_tier=source_tier,
                                target_tier=next_tier,
                                reason=reason,
                                recipient_roles=recipient_roles,
                            )
                        except Exception as exc:
                            logger.error(
                                f"Failed to dispatch escalation alert for ticket {ticket.id}: {exc}"
                            )

    return result


def evaluate_all_active_slas(
    session: Session,
    notification_port: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Batch evaluate all active support tickets for SLA countdowns, breaches, and escalations [FR-4, FR-5, FR-6].

    Args:
        session: Active synchronous database session.
        notification_port: Optional notification port adapter instance.
        now: Reference evaluation timestamp (defaults to current UTC time).

    Returns:
        Dict summarizing aggregate batch scan results.
    """
    eval_now = _ensure_utc(now) or datetime.now(timezone.utc)
    stmt = select(SupportTicket).where(SupportTicket.status.in_(["open", "pending"]))
    active_tickets = list(session.scalars(stmt).all())

    warnings_count = 0
    breaches_count = 0
    escalations_count = 0
    details: list[dict[str, Any]] = []

    for ticket in active_tickets:
        ticket_eval = evaluate_ticket_sla(
            session=session,
            ticket=ticket,
            notification_port=notification_port,
            now=eval_now,
        )

        warnings_count += len(ticket_eval["warnings_sent"])
        breaches_count += len(ticket_eval["breaches_detected"])
        if ticket_eval["escalated"]:
            escalations_count += 1

        details.append(ticket_eval)

    session.flush()

    logger.info(
        json.dumps(
            {
                "event": "batch_sla_evaluation_completed",
                "scanned_tickets_count": len(active_tickets),
                "warnings_count": warnings_count,
                "breaches_count": breaches_count,
                "escalations_count": escalations_count,
                "correlation_id": get_correlation_id(),
            }
        )
    )

    return {
        "evaluated_count": len(active_tickets),
        "warnings_triggered_count": warnings_count,
        "breaches_triggered_count": breaches_count,
        "escalations_triggered_count": escalations_count,
        "details": details,
    }


class SLAService:
    """Domain service encapsulating support SLA evaluation and escalation operations [NFR-4]."""

    def __init__(
        self,
        session: Session,
        notification_port: Any | None = None,
    ) -> None:
        """Initialize SLAService with explicit dependency injection.

        Args:
            session: Active synchronous database session.
            notification_port: Optional notification connector/port instance.
        """
        self.session = session
        self.notification_port = notification_port

    def calculate_deadlines(
        self,
        priority: str,
        created_at: datetime | None = None,
    ) -> dict[str, datetime]:
        """Calculate target due datetimes for first_response and resolution SLA targets."""
        return calculate_target_deadlines(priority=priority, created_at=created_at)

    def initialize_tracking(self, ticket: SupportTicket) -> list[SLATracking]:
        """Initialize SLA tracking records for a support ticket."""
        return initialize_sla_tracking(session=self.session, ticket=ticket)

    def recalculate_priority(
        self,
        ticket: SupportTicket,
        new_priority: str,
        now: datetime | None = None,
    ) -> list[SLATracking]:
        """Recalculate SLA target due datetimes upon ticket priority change."""
        return recalculate_sla_targets(
            session=self.session,
            ticket=ticket,
            new_priority=new_priority,
            now=now,
        )

    def evaluate_ticket(
        self,
        ticket: SupportTicket,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Evaluate SLA countdowns, warnings, breaches, and escalations for a ticket."""
        return evaluate_ticket_sla(
            session=self.session,
            ticket=ticket,
            notification_port=self.notification_port,
            now=now,
        )

    def evaluate_all_active(self, now: datetime | None = None) -> dict[str, Any]:
        """Batch evaluate all active support tickets for SLA countdowns and escalations."""
        return evaluate_all_active_slas(
            session=self.session,
            notification_port=self.notification_port,
            now=now,
        )
