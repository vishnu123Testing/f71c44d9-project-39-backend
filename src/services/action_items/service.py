"""Action Item Service implementation for action item tracking, SLA breach scanning, and escalation management.

This module encapsulates business logic for creating action items, assigning owners and target due dates,
monitoring SLA breach thresholds, completing action items, clearing escalation states, and emitting
structured context-correlated JSON logs (FR-004, FR-005, NFR-004).
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Callable
import uuid
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.lib.config import settings
from src.lib.logging import get_correlation_id
from src.models.deal import ActionItem, Deal

logger = logging.getLogger(__name__)


class ActionItemNotFoundError(ValueError):
    """Raised when a requested action item record is not found in the database."""

    pass


class InvalidActionItemDataError(ValueError):
    """Raised when action item input parameters fail domain validation rules."""

    pass


class DealNotFoundError(ValueError):
    """Raised when the specified deal for an action item does not exist."""

    pass


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Ensure a datetime instance is timezone-aware in UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class ActionItemDomain:
    """Pure domain entity representation of an Action Item."""

    id: UUID
    deal_id: UUID
    title: str
    assigned_owner_id: str
    target_due_date: datetime
    sla_threshold_hours: int
    is_completed: bool
    completed_on: datetime | None
    is_escalated: bool
    escalated_on: datetime | None
    created_on: datetime

    @property
    def is_overdue(self) -> bool:
        """Return True if the action item is incomplete and target due date has passed."""
        if self.is_completed:
            return False
        now_utc = datetime.now(timezone.utc)
        due_utc = _ensure_utc(self.target_due_date)
        return due_utc is not None and now_utc > due_utc


@dataclass
class SLABreachScanResultDTO:
    """Data transfer object summarizing the results of an SLA breach scan."""

    scanned_count: int
    newly_escalated_count: int
    already_escalated_count: int
    escalated_items: list[ActionItemDomain] = field(default_factory=list)


def to_action_item_domain(action_item: ActionItem) -> ActionItemDomain:
    """Map a SQLAlchemy ActionItem ORM instance to a pure ActionItemDomain entity."""
    return ActionItemDomain(
        id=action_item.id,
        deal_id=action_item.deal_id,
        title=action_item.title,
        assigned_owner_id=action_item.assigned_owner_id,
        target_due_date=_ensure_utc(action_item.target_due_date)
        or action_item.target_due_date,
        sla_threshold_hours=action_item.sla_threshold_hours,
        is_completed=action_item.is_completed,
        completed_on=_ensure_utc(action_item.completed_on),
        is_escalated=action_item.is_escalated,
        escalated_on=_ensure_utc(action_item.escalated_on),
        created_on=_ensure_utc(action_item.created_on)
        or action_item.created_on,
    )


def create_action_item(
    session: Session,
    deal_id: UUID | str,
    title: str,
    assigned_owner_id: str,
    target_due_date: datetime | str,
    sla_threshold_hours: int | None = None,
) -> ActionItemDomain:
    """Create and persist a new action item assigned to a deal with SLA configuration."""
    if isinstance(deal_id, str):
        try:
            deal_uuid = UUID(deal_id)
        except ValueError as err:
            raise InvalidActionItemDataError(
                f"Invalid deal ID format: {deal_id}"
            ) from err
    else:
        deal_uuid = deal_id

    deal = session.get(Deal, deal_uuid)
    if deal is None:
        raise DealNotFoundError(f"Deal with ID {deal_id} not found.")

    title_clean = title.strip() if isinstance(title, str) else ""
    if not title_clean:
        raise InvalidActionItemDataError("Action item title cannot be empty.")

    owner_clean = (
        assigned_owner_id.strip()
        if isinstance(assigned_owner_id, str)
        else ""
    )
    if not owner_clean:
        raise InvalidActionItemDataError("Assigned owner ID cannot be empty.")

    if isinstance(target_due_date, str):
        try:
            due_dt = datetime.fromisoformat(target_due_date)
        except ValueError as err:
            raise InvalidActionItemDataError(
                f"Invalid target due date format: {target_due_date}"
            ) from err
    elif isinstance(target_due_date, datetime):
        due_dt = target_due_date
    else:
        raise InvalidActionItemDataError(
            "Target due date must be a datetime or ISO string."
        )

    due_dt = _ensure_utc(due_dt)
    if due_dt is None:
        raise InvalidActionItemDataError("Target due date cannot be None.")

    if sla_threshold_hours is None or sla_threshold_hours <= 0:
        sla_hours = int(getattr(settings, "DEFAULT_SLA_THRESHOLD_HOURS", 24))
    else:
        sla_hours = int(sla_threshold_hours)

    now_utc = datetime.now(timezone.utc)
    item = ActionItem(
        deal_id=deal_uuid,
        title=title_clean,
        assigned_owner_id=owner_clean,
        target_due_date=due_dt,
        sla_threshold_hours=sla_hours,
        is_completed=False,
        completed_on=None,
        is_escalated=False,
        escalated_on=None,
        created_on=now_utc,
    )
    session.add(item)
    session.commit()
    session.refresh(item)

    domain_item = to_action_item_domain(item)
    logger.info(
        "Action item created successfully",
        extra={
            "event_type": "action_item_created",
            "action_item_id": str(item.id),
            "deal_id": str(item.deal_id),
            "title": item.title,
            "assigned_owner_id": item.assigned_owner_id,
            "target_due_date": due_dt.isoformat(),
            "sla_threshold_hours": sla_hours,
            "correlation_id": get_correlation_id(),
        },
    )
    return domain_item


def get_action_item_by_id(
    session: Session,
    item_id: UUID | str,
) -> ActionItemDomain:
    """Retrieve an action item by ID."""
    if isinstance(item_id, str):
        try:
            item_uuid = UUID(item_id)
        except ValueError as err:
            raise InvalidActionItemDataError(
                f"Invalid action item ID format: {item_id}"
            ) from err
    else:
        item_uuid = item_id

    item = session.get(ActionItem, item_uuid)
    if item is None:
        raise ActionItemNotFoundError(f"Action item with ID {item_id} not found.")

    return to_action_item_domain(item)


def get_action_items_for_deal(
    session: Session,
    deal_id: UUID | str,
    include_completed: bool = True,
) -> list[ActionItemDomain]:
    """Retrieve all action items assigned to a specific deal."""
    if isinstance(deal_id, str):
        try:
            deal_uuid = UUID(deal_id)
        except ValueError as err:
            raise InvalidActionItemDataError(
                f"Invalid deal ID format: {deal_id}"
            ) from err
    else:
        deal_uuid = deal_id

    stmt = select(ActionItem).where(ActionItem.deal_id == deal_uuid)
    if not include_completed:
        stmt = stmt.where(ActionItem.is_completed == False)

    stmt = stmt.order_by(
        ActionItem.target_due_date.asc(), ActionItem.created_on.asc()
    )
    items = session.scalars(stmt).all()
    return [to_action_item_domain(item) for item in items]


def complete_action_item(
    session: Session,
    item_id: UUID | str,
    notification_dispatcher: Callable[[dict[str, Any]], None] | None = None,
) -> ActionItemDomain:
    """Mark an action item as completed, clear any escalation state, and record completion event."""
    if isinstance(item_id, str):
        try:
            item_uuid = UUID(item_id)
        except ValueError as err:
            raise InvalidActionItemDataError(
                f"Invalid action item ID format: {item_id}"
            ) from err
    else:
        item_uuid = item_id

    item = session.get(ActionItem, item_uuid)
    if item is None:
        raise ActionItemNotFoundError(f"Action item with ID {item_id} not found.")

    now_utc = datetime.now(timezone.utc)
    item.is_completed = True
    item.completed_on = now_utc
    was_escalated = item.is_escalated
    item.is_escalated = False

    session.commit()
    session.refresh(item)

    domain_item = to_action_item_domain(item)
    correlation_id = get_correlation_id()

    logger.info(
        "Action item completed and escalation cleared",
        extra={
            "event_type": "action_item_completed",
            "action_item_id": str(item.id),
            "deal_id": str(item.deal_id),
            "assigned_owner_id": item.assigned_owner_id,
            "completed_on": now_utc.isoformat(),
            "was_escalated": was_escalated,
            "correlation_id": correlation_id,
        },
    )

    if notification_dispatcher is not None:
        try:
            payload = {
                "event_type": "action_item_completed",
                "action_item_id": str(item.id),
                "deal_id": str(item.deal_id),
                "assigned_owner_id": item.assigned_owner_id,
                "completed_on": now_utc.isoformat(),
                "was_escalated": was_escalated,
                "correlation_id": correlation_id,
            }
            notification_dispatcher(payload)
        except Exception as exc:
            logger.warning(
                "Notification dispatcher failed during action item completion",
                extra={
                    "action_item_id": str(item.id),
                    "error": str(exc),
                    "correlation_id": correlation_id,
                },
            )

    return domain_item


def delete_action_item(
    session: Session,
    item_id: UUID | str,
) -> bool:
    """Delete an action item by ID."""
    if isinstance(item_id, str):
        try:
            item_uuid = UUID(item_id)
        except ValueError as err:
            raise InvalidActionItemDataError(
                f"Invalid action item ID format: {item_id}"
            ) from err
    else:
        item_uuid = item_id

    item = session.get(ActionItem, item_uuid)
    if item is None:
        raise ActionItemNotFoundError(f"Action item with ID {item_id} not found.")

    session.delete(item)
    session.commit()

    logger.info(
        "Action item deleted",
        extra={
            "event_type": "action_item_deleted",
            "action_item_id": str(item_uuid),
            "correlation_id": get_correlation_id(),
        },
    )
    return True


def scan_and_escalate_sla_breaches(
    session: Session,
    reference_time: datetime | None = None,
    notification_dispatcher: Callable[[dict[str, Any]], None] | None = None,
) -> SLABreachScanResultDTO:
    """Scan uncompleted action items for SLA threshold breaches and trigger escalation alerts.

    Action items that are already escalated suppress duplicate escalation notifications.
    """
    now_utc = (
        _ensure_utc(reference_time)
        if reference_time is not None
        else datetime.now(timezone.utc)
    )
    correlation_id = get_correlation_id()

    stmt = select(ActionItem).where(ActionItem.is_completed == False)
    uncompleted_items = session.scalars(stmt).all()

    scanned_count = len(uncompleted_items)
    newly_escalated_count = 0
    already_escalated_count = 0
    escalated_domains: list[ActionItemDomain] = []

    recipient_roles = getattr(
        settings, "ESCALATION_RECIPIENT_ROLES", ["SalesManager", "Admin"]
    )

    for item in uncompleted_items:
        due_dt = _ensure_utc(item.target_due_date)
        created_dt = _ensure_utc(item.created_on) or now_utc
        sla_deadline = created_dt + timedelta(hours=item.sla_threshold_hours)

        is_breached = (due_dt is not None and now_utc >= due_dt) or (
            now_utc >= sla_deadline
        )

        if is_breached:
            if item.is_escalated:
                already_escalated_count += 1
                logger.debug(
                    "Action item SLA breach active; duplicate escalation alert suppressed",
                    extra={
                        "event_type": "action_item_escalation_suppressed",
                        "action_item_id": str(item.id),
                        "deal_id": str(item.deal_id),
                        "assigned_owner_id": item.assigned_owner_id,
                        "correlation_id": correlation_id,
                    },
                )
            else:
                item.is_escalated = True
                item.escalated_on = now_utc
                newly_escalated_count += 1

                domain_item = to_action_item_domain(item)
                escalated_domains.append(domain_item)

                logger.warning(
                    "Action item SLA threshold breached; triggering escalation alert",
                    extra={
                        "event_type": "action_item_sla_escalation",
                        "action_item_id": str(item.id),
                        "deal_id": str(item.deal_id),
                        "title": item.title,
                        "assigned_owner_id": item.assigned_owner_id,
                        "target_due_date": due_dt.isoformat() if due_dt else None,
                        "sla_threshold_hours": item.sla_threshold_hours,
                        "escalated_on": now_utc.isoformat(),
                        "recipient_roles": recipient_roles,
                        "correlation_id": correlation_id,
                    },
                )

                if notification_dispatcher is not None:
                    try:
                        payload = {
                            "event_type": "action_item_sla_escalation",
                            "action_item_id": str(item.id),
                            "deal_id": str(item.deal_id),
                            "title": item.title,
                            "assigned_owner_id": item.assigned_owner_id,
                            "target_due_date": due_dt.isoformat()
                            if due_dt
                            else None,
                            "sla_threshold_hours": item.sla_threshold_hours,
                            "escalated_on": now_utc.isoformat(),
                            "recipient_roles": recipient_roles,
                            "correlation_id": correlation_id,
                        }
                        notification_dispatcher(payload)
                    except Exception as exc:
                        logger.warning(
                            "Notification dispatcher failed during SLA escalation",
                            extra={
                                "action_item_id": str(item.id),
                                "error": str(exc),
                                "correlation_id": correlation_id,
                            },
                        )

    if newly_escalated_count > 0:
        session.commit()

    return SLABreachScanResultDTO(
        scanned_count=scanned_count,
        newly_escalated_count=newly_escalated_count,
        already_escalated_count=already_escalated_count,
        escalated_items=escalated_domains,
    )


def clear_escalation(
    session: Session,
    item_id: UUID | str,
) -> ActionItemDomain:
    """Clear escalation state for an action item."""
    if isinstance(item_id, str):
        try:
            item_uuid = UUID(item_id)
        except ValueError as err:
            raise InvalidActionItemDataError(
                f"Invalid action item ID format: {item_id}"
            ) from err
    else:
        item_uuid = item_id

    item = session.get(ActionItem, item_uuid)
    if item is None:
        raise ActionItemNotFoundError(f"Action item with ID {item_id} not found.")

    item.is_escalated = False
    item.escalated_on = None

    session.commit()
    session.refresh(item)

    domain_item = to_action_item_domain(item)
    logger.info(
        "Action item escalation state cleared",
        extra={
            "event_type": "action_item_escalation_cleared",
            "action_item_id": str(item.id),
            "deal_id": str(item.deal_id),
            "correlation_id": get_correlation_id(),
        },
    )
    return domain_item


class ActionItemService:
    """Service class encapsulating Action Item management and SLA escalation rules."""

    def __init__(self, session: Session):
        self.session = session

    def create(
        self,
        deal_id: UUID | str,
        title: str,
        assigned_owner_id: str,
        target_due_date: datetime | str,
        sla_threshold_hours: int | None = None,
    ) -> ActionItemDomain:
        """Create a new action item."""
        return create_action_item(
            self.session,
            deal_id=deal_id,
            title=title,
            assigned_owner_id=assigned_owner_id,
            target_due_date=target_due_date,
            sla_threshold_hours=sla_threshold_hours,
        )

    def get_by_id(self, item_id: UUID | str) -> ActionItemDomain:
        """Get an action item by ID."""
        return get_action_item_by_id(self.session, item_id=item_id)

    def get_for_deal(
        self,
        deal_id: UUID | str,
        include_completed: bool = True,
    ) -> list[ActionItemDomain]:
        """Get action items for a deal."""
        return get_action_items_for_deal(
            self.session, deal_id=deal_id, include_completed=include_completed
        )

    def complete(
        self,
        item_id: UUID | str,
        notification_dispatcher: Callable[[dict[str, Any]], None] | None = None,
    ) -> ActionItemDomain:
        """Complete an action item and clear its escalation state."""
        return complete_action_item(
            self.session,
            item_id=item_id,
            notification_dispatcher=notification_dispatcher,
        )

    def delete(self, item_id: UUID | str) -> bool:
        """Delete an action item by ID."""
        return delete_action_item(self.session, item_id=item_id)

    def scan_sla_breaches(
        self,
        reference_time: datetime | None = None,
        notification_dispatcher: Callable[[dict[str, Any]], None] | None = None,
    ) -> SLABreachScanResultDTO:
        """Scan and escalate SLA breaches."""
        return scan_and_escalate_sla_breaches(
            self.session,
            reference_time=reference_time,
            notification_dispatcher=notification_dispatcher,
        )

    def clear_escalation_state(self, item_id: UUID | str) -> ActionItemDomain:
        """Clear escalation state for an action item."""
        return clear_escalation(self.session, item_id=item_id)
