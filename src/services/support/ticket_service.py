"""Ticket Service implementation for support operations and lifecycle management [FR-1, FR-2, FR-3, FR-7, FR-8, FR-9, NFR-3, NFR-4].

This module encapsulates core domain business rules for ticket creation, retrieval, filtering,
status updates, sequential message threading, attachment binding, customer satisfaction (CSAT) scoring,
and operational metrics aggregation.
"""

from datetime import datetime, timezone
import json
import logging
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from src.lib.logging import get_correlation_id
from src.models.ticket import (
    CSATRating,
    SLATracking,
    SupportTicket,
    TicketAttachment,
    TicketMessage,
)
from src.services.support.sla_service import (
    initialize_sla_tracking,
    recalculate_sla_targets,
)

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


def create_ticket(
    session: Session,
    subject: str,
    description: str,
    client_id: str,
    priority: str = "P3",
    assigned_tier: str = "Tier 1",
    assigned_agent_id: str | None = None,
    created_at: datetime | None = None,
    storage_port: Any | None = None,
    initial_attachments: list[dict[str, Any]] | None = None,
) -> SupportTicket:
    """Create a new client support ticket, initialize SLA tracking, and log initial message [FR-1, FR-2, FR-7].

    Args:
        session: Active synchronous database session.
        subject: Brief subject line describing the issue.
        description: Detailed explanation of the support issue.
        client_id: Identifier of the client/customer requesting support.
        priority: Priority code ("P1", "P2", "P3"). Defaults to "P3".
        assigned_tier: Support tier assigned to handle the ticket. Defaults to "Tier 1".
        assigned_agent_id: Optional ID of the support agent assigned.
        created_at: Optional timestamp override for ticket creation (UTC).
        storage_port: Optional storage port connector for processing attachments.
        initial_attachments: Optional list of attachment dicts (content bytes or pre-stored keys).

    Returns:
        The newly created SupportTicket ORM instance.
    """
    norm_priority = priority.strip().upper() if priority else "P3"
    if norm_priority not in ("P1", "P2", "P3"):
        norm_priority = "P3"

    now_utc = _ensure_utc(created_at) or datetime.now(timezone.utc)
    ticket_id = uuid4()

    ticket = SupportTicket(
        id=ticket_id,
        priority=norm_priority,
        status="open",
        subject=subject.strip(),
        description=description.strip(),
        client_id=client_id,
        assigned_tier=assigned_tier,
        assigned_agent_id=assigned_agent_id,
        created_at=now_utc,
        updated_at=now_utc,
    )
    session.add(ticket)

    # Initialize SLA target tracking
    initialize_sla_tracking(session, ticket)

    # Create initial thread message representing the ticket description
    initial_msg = TicketMessage(
        id=uuid4(),
        ticket_id=ticket.id,
        sender_id=client_id,
        sender_type="client",
        message_body=description.strip(),
        created_at=now_utc,
    )
    session.add(initial_msg)

    # Process any initial attachments
    if initial_attachments:
        for att in initial_attachments:
            filename = att.get("filename", "attachment.bin")
            content = att.get("content")
            storage_key = att.get("storage_key")
            file_size_bytes = att.get("file_size_bytes", 0)
            mime_type = att.get("mime_type", "application/octet-stream")

            if content is not None and storage_port is not None:
                val_res = storage_port.validate_file(
                    filename=filename,
                    file_size_bytes=len(content),
                    mime_type=mime_type,
                    content=content,
                )
                if hasattr(val_res, "is_valid") and not val_res.is_valid:
                    error_msg = getattr(val_res, "error_message", "Validation failed")
                    raise ValueError(f"Attachment validation failed: {error_msg}")

                stored = storage_port.store_file(
                    filename=filename,
                    content=content,
                    mime_type=mime_type,
                )
                storage_key = stored.storage_key
                file_size_bytes = stored.file_size_bytes
                mime_type = stored.mime_type

            if not storage_key:
                raise ValueError("Attachment missing storage_key or content for upload")

            attachment_rec = TicketAttachment(
                id=uuid4(),
                message_id=initial_msg.id,
                filename=filename,
                file_size_bytes=file_size_bytes,
                mime_type=mime_type,
                storage_key=storage_key,
                uploaded_at=now_utc,
            )
            session.add(attachment_rec)

    logger.info(
        json.dumps(
            {
                "event": "ticket_created",
                "ticket_id": str(ticket.id),
                "client_id": client_id,
                "priority": norm_priority,
                "assigned_tier": assigned_tier,
                "created_at": now_utc.isoformat(),
                "correlation_id": get_correlation_id(),
            }
        )
    )

    session.flush()
    return ticket


def get_ticket(
    session: Session,
    ticket_id: UUID | str,
) -> SupportTicket | None:
    """Retrieve a support ticket by its unique identifier [FR-1].

    Args:
        session: Active synchronous database session.
        ticket_id: UUID or string UUID of the ticket.

    Returns:
        SupportTicket ORM instance or None if not found.
    """
    uid = ticket_id if isinstance(ticket_id, UUID) else UUID(str(ticket_id))
    return session.get(SupportTicket, uid)


def list_tickets(
    session: Session,
    client_id: str | None = None,
    priority: str | None = None,
    status: str | None = None,
    assigned_tier: str | None = None,
    assigned_agent_id: str | None = None,
    search_query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[SupportTicket], int]:
    """Retrieve a paginated list of support tickets matching search and filter criteria [FR-1].

    Args:
        session: Active synchronous database session.
        client_id: Optional client identifier filter.
        priority: Optional priority filter ("P1", "P2", "P3").
        status: Optional status filter ("open", "pending", "resolved", "closed").
        assigned_tier: Optional tier filter ("Tier 1", "Tier 2", "Tier 3").
        assigned_agent_id: Optional assigned agent ID filter.
        search_query: Optional search substring for subject/description.
        limit: Maximum record count to return. Defaults to 50.
        offset: Offset integer for pagination. Defaults to 0.

    Returns:
        Tuple containing list of matching SupportTicket instances and total matching record count.
    """
    stmt = select(SupportTicket)

    if client_id:
        stmt = stmt.where(SupportTicket.client_id == client_id)
    if priority:
        stmt = stmt.where(SupportTicket.priority == priority.strip().upper())
    if status:
        stmt = stmt.where(SupportTicket.status == status.strip().lower())
    if assigned_tier:
        stmt = stmt.where(SupportTicket.assigned_tier == assigned_tier)
    if assigned_agent_id:
        stmt = stmt.where(SupportTicket.assigned_agent_id == assigned_agent_id)
    if search_query and search_query.strip():
        q = f"%{search_query.strip()}%"
        stmt = stmt.where(
            or_(
                SupportTicket.subject.ilike(q),
                SupportTicket.description.ilike(q),
            )
        )

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total_count = session.scalar(count_stmt) or 0

    stmt = stmt.order_by(SupportTicket.created_at.desc()).offset(offset).limit(limit)
    tickets = list(session.scalars(stmt).all())

    return tickets, total_count


def update_ticket(
    session: Session,
    ticket_id: UUID | str,
    status: str | None = None,
    priority: str | None = None,
    assigned_tier: str | None = None,
    assigned_agent_id: str | None = None,
    subject: str | None = None,
    description: str | None = None,
    updated_at: datetime | None = None,
) -> SupportTicket:
    """Update support ticket status, priority, assignments, or details [FR-1, FR-7].

    Args:
        session: Active synchronous database session.
        ticket_id: UUID or string UUID of the ticket.
        status: Optional new ticket status.
        priority: Optional new priority code ("P1", "P2", "P3").
        assigned_tier: Optional new support tier assignment.
        assigned_agent_id: Optional assigned agent ID.
        subject: Optional updated subject line.
        description: Optional updated description text.
        updated_at: Optional timestamp override for update (UTC).

    Returns:
        Updated SupportTicket instance.

    Raises:
        ValueError: If ticket is not found.
    """
    ticket = get_ticket(session, ticket_id)
    if not ticket:
        raise ValueError(f"Ticket not found: {ticket_id}")

    now_utc = _ensure_utc(updated_at) or datetime.now(timezone.utc)
    old_status = ticket.status
    old_priority = ticket.priority

    if priority and priority.strip().upper() != ticket.priority:
        new_priority = priority.strip().upper()
        if new_priority in ("P1", "P2", "P3"):
            recalculate_sla_targets(session, ticket, new_priority, now=now_utc)

    if status:
        norm_status = status.strip().lower()
        if norm_status == "resolved" and ticket.status != "resolved":
            ticket.resolved_at = now_utc

            # Check if resolution SLA target was breached at point of resolution
            for tracking in ticket.sla_trackings:
                if tracking.target_type == "resolution":
                    target_due = _ensure_utc(tracking.target_due_at)
                    if target_due and now_utc > target_due:
                        tracking.is_breached = True
                        tracking.breached_at = tracking.breached_at or target_due
                    else:
                        tracking.is_breached = False

        elif norm_status in ("open", "pending") and ticket.status in ("resolved", "closed"):
            ticket.resolved_at = None

        ticket.status = norm_status

    if assigned_tier:
        ticket.assigned_tier = assigned_tier
    if assigned_agent_id is not None:
        ticket.assigned_agent_id = assigned_agent_id
    if subject:
        ticket.subject = subject.strip()
    if description:
        ticket.description = description.strip()

    ticket.updated_at = now_utc

    logger.info(
        json.dumps(
            {
                "event": "ticket_updated",
                "ticket_id": str(ticket.id),
                "client_id": ticket.client_id,
                "old_status": old_status,
                "new_status": ticket.status,
                "old_priority": old_priority,
                "new_priority": ticket.priority,
                "updated_at": now_utc.isoformat(),
                "correlation_id": get_correlation_id(),
            }
        )
    )

    session.flush()
    return ticket


def delete_ticket(
    session: Session,
    ticket_id: UUID | str,
) -> bool:
    """Delete a support ticket record and associated child entities [FR-1].

    Args:
        session: Active synchronous database session.
        ticket_id: UUID or string UUID of ticket to delete.

    Returns:
        True if successfully deleted, False if ticket was not found.
    """
    ticket = get_ticket(session, ticket_id)
    if not ticket:
        return False

    session.delete(ticket)

    logger.info(
        json.dumps(
            {
                "event": "ticket_deleted",
                "ticket_id": str(ticket_id),
                "correlation_id": get_correlation_id(),
            }
        )
    )

    session.flush()
    return True


def add_message(
    session: Session,
    ticket_id: UUID | str,
    sender_id: str,
    sender_type: str,
    message_body: str,
    attachments: list[dict[str, Any]] | None = None,
    storage_port: Any | None = None,
    created_at: datetime | None = None,
) -> TicketMessage:
    """Append a message to a ticket's sequential thread with optional file attachments [FR-2, FR-3, FR-7].

    Args:
        session: Active synchronous database session.
        ticket_id: UUID or string UUID of target ticket.
        sender_id: ID of the message author (client, agent, or system).
        sender_type: Type code ("client", "agent", or "system").
        message_body: Text contents of the message.
        attachments: Optional list of file attachment dicts.
        storage_port: Optional storage port adapter for file upload validation and storage.
        created_at: Optional timestamp override for message creation (UTC).

    Returns:
        The created TicketMessage instance.

    Raises:
        ValueError: If ticket is not found or file validation fails.
    """
    ticket = get_ticket(session, ticket_id)
    if not ticket:
        raise ValueError(f"Ticket not found: {ticket_id}")

    msg_now = _ensure_utc(created_at) or datetime.now(timezone.utc)
    norm_sender_type = sender_type.strip().lower()

    # Track first agent response timestamp and first_response SLA
    if norm_sender_type == "agent" and ticket.first_responded_at is None:
        ticket.first_responded_at = msg_now

        for tracking in ticket.sla_trackings:
            if tracking.target_type == "first_response":
                target_due = _ensure_utc(tracking.target_due_at)
                if target_due and msg_now > target_due:
                    tracking.is_breached = True
                    tracking.breached_at = tracking.breached_at or target_due
                else:
                    tracking.is_breached = False

        logger.info(
            json.dumps(
                {
                    "event": "first_agent_response_logged",
                    "ticket_id": str(ticket.id),
                    "client_id": ticket.client_id,
                    "first_responded_at": msg_now.isoformat(),
                    "correlation_id": get_correlation_id(),
                }
            )
        )

    ticket.updated_at = msg_now

    message = TicketMessage(
        id=uuid4(),
        ticket_id=ticket.id,
        sender_id=sender_id,
        sender_type=norm_sender_type,
        message_body=message_body.strip(),
        created_at=msg_now,
    )
    session.add(message)

    created_attachments: list[TicketAttachment] = []
    if attachments:
        for att in attachments:
            filename = att.get("filename", "attachment.bin")
            content = att.get("content")
            storage_key = att.get("storage_key")
            file_size_bytes = att.get("file_size_bytes", 0)
            mime_type = att.get("mime_type", "application/octet-stream")

            if content is not None and storage_port is not None:
                val_res = storage_port.validate_file(
                    filename=filename,
                    file_size_bytes=len(content),
                    mime_type=mime_type,
                    content=content,
                )
                if hasattr(val_res, "is_valid") and not val_res.is_valid:
                    error_msg = getattr(val_res, "error_message", "Validation failed")
                    raise ValueError(f"Attachment validation failed: {error_msg}")

                stored = storage_port.store_file(
                    filename=filename,
                    content=content,
                    mime_type=mime_type,
                )
                storage_key = stored.storage_key
                file_size_bytes = stored.file_size_bytes
                mime_type = stored.mime_type

            if not storage_key:
                raise ValueError("Attachment missing storage_key or content for upload")

            attachment_rec = TicketAttachment(
                id=uuid4(),
                message_id=message.id,
                filename=filename,
                file_size_bytes=file_size_bytes,
                mime_type=mime_type,
                storage_key=storage_key,
                uploaded_at=msg_now,
            )
            session.add(attachment_rec)
            created_attachments.append(attachment_rec)

    logger.info(
        json.dumps(
            {
                "event": "ticket_message_added",
                "ticket_id": str(ticket.id),
                "message_id": str(message.id),
                "sender_id": sender_id,
                "sender_type": norm_sender_type,
                "attachment_count": len(created_attachments),
                "correlation_id": get_correlation_id(),
            }
        )
    )

    session.flush()
    return message


def get_ticket_messages(
    session: Session,
    ticket_id: UUID | str,
) -> list[TicketMessage]:
    """Retrieve sequential message history for a ticket ordered chronologically [FR-2].

    Args:
        session: Active synchronous database session.
        ticket_id: UUID or string UUID of ticket.

    Returns:
        List of TicketMessage instances ordered by created_at ascending.
    """
    uid = ticket_id if isinstance(ticket_id, UUID) else UUID(str(ticket_id))
    stmt = (
        select(TicketMessage)
        .where(TicketMessage.ticket_id == uid)
        .order_by(TicketMessage.created_at.asc())
    )
    return list(session.scalars(stmt).all())


def submit_csat_rating(
    session: Session,
    ticket_id: UUID | str,
    score: int,
    feedback_text: str | None = None,
    submitted_at: datetime | None = None,
) -> CSATRating:
    """Submit customer satisfaction rating for a resolved support ticket [FR-9].

    Args:
        session: Active synchronous database session.
        ticket_id: UUID or string UUID of resolved ticket.
        score: Numerical score rating between 1 and 5.
        feedback_text: Optional commentary or feedback text.
        submitted_at: Optional timestamp override for submission (UTC).

    Returns:
        Created CSATRating instance.

    Raises:
        ValueError: If ticket is not found, score is out of range 1-5, ticket is not resolved/closed,
                    or CSAT rating was already submitted for this ticket.
    """
    ticket = get_ticket(session, ticket_id)
    if not ticket:
        raise ValueError(f"Ticket not found: {ticket_id}")

    if score < 1 or score > 5:
        raise ValueError("CSAT score must be an integer between 1 and 5")

    if ticket.status not in ("resolved", "closed"):
        raise ValueError("CSAT rating can only be submitted for resolved or closed tickets")

    if ticket.csat_rating is not None:
        raise ValueError("CSAT rating has already been submitted for this ticket")

    sub_now = _ensure_utc(submitted_at) or datetime.now(timezone.utc)

    rating = CSATRating(
        id=uuid4(),
        ticket_id=ticket.id,
        score=score,
        feedback_text=feedback_text.strip() if feedback_text else None,
        submitted_at=sub_now,
    )
    session.add(rating)

    logger.info(
        json.dumps(
            {
                "event": "csat_rating_submitted",
                "ticket_id": str(ticket.id),
                "client_id": ticket.client_id,
                "score": score,
                "correlation_id": get_correlation_id(),
            }
        )
    )

    session.flush()
    return rating


def get_csat_rating(
    session: Session,
    ticket_id: UUID | str,
) -> CSATRating | None:
    """Retrieve CSAT rating submitted for a ticket [FR-9].

    Args:
        session: Active synchronous database session.
        ticket_id: UUID or string UUID of ticket.

    Returns:
        CSATRating instance or None if no rating submitted.
    """
    uid = ticket_id if isinstance(ticket_id, UUID) else UUID(str(ticket_id))
    return session.scalar(select(CSATRating).where(CSATRating.ticket_id == uid))


def get_support_analytics(
    session: Session,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    priority: str | None = None,
    client_id: str | None = None,
) -> dict[str, Any]:
    """Aggregate operational support analytics across a specified time window [FR-8].

    Calculates total ticket volume, status breakdown, average first response time,
    mean time to resolution (MTTR), SLA target breach rate, and CSAT scores.

    Args:
        session: Active synchronous database session.
        start_time: Optional window start datetime (UTC).
        end_time: Optional window end datetime (UTC).
        priority: Optional priority filter ("P1", "P2", "P3").
        client_id: Optional client identifier filter.

    Returns:
        Dict containing aggregated operational metrics.
    """
    start_time_utc = _ensure_utc(start_time)
    end_time_utc = _ensure_utc(end_time)

    stmt = select(SupportTicket)

    if start_time_utc:
        stmt = stmt.where(SupportTicket.created_at >= start_time_utc)
    if end_time_utc:
        stmt = stmt.where(SupportTicket.created_at <= end_time_utc)
    if priority:
        stmt = stmt.where(SupportTicket.priority == priority.strip().upper())
    if client_id:
        stmt = stmt.where(SupportTicket.client_id == client_id)

    tickets = list(session.scalars(stmt).all())
    total_tickets = len(tickets)

    open_tickets = sum(1 for t in tickets if t.status == "open")
    pending_tickets = sum(1 for t in tickets if t.status == "pending")
    resolved_tickets = sum(1 for t in tickets if t.status in ("resolved", "closed"))

    # Calculate Average First Response Time (seconds)
    responded_times = [
        (_ensure_utc(t.first_responded_at) - _ensure_utc(t.created_at)).total_seconds()
        for t in tickets
        if t.first_responded_at is not None
    ]
    avg_first_response = (
        sum(responded_times) / len(responded_times) if responded_times else 0.0
    )

    # Calculate Mean Time To Resolution / MTTR (seconds)
    resolved_times = [
        (_ensure_utc(t.resolved_at) - _ensure_utc(t.created_at)).total_seconds()
        for t in tickets
        if t.resolved_at is not None
    ]
    mttr = sum(resolved_times) / len(resolved_times) if resolved_times else 0.0

    # Calculate SLA Breach Rate across matching tickets
    ticket_ids = [t.id for t in tickets]
    if ticket_ids:
        trackings = list(
            session.scalars(
                select(SLATracking).where(SLATracking.ticket_id.in_(ticket_ids))
            ).all()
        )
        total_sla_targets = len(trackings)
        breached_sla_targets = sum(1 for tr in trackings if tr.is_breached)
        breach_rate = (
            (breached_sla_targets / total_sla_targets * 100.0)
            if total_sla_targets > 0
            else 0.0
        )
    else:
        total_sla_targets = 0
        breached_sla_targets = 0
        breach_rate = 0.0

    # Calculate CSAT score metrics
    if ticket_ids:
        csats = list(
            session.scalars(
                select(CSATRating).where(CSATRating.ticket_id.in_(ticket_ids))
            ).all()
        )
        csat_count = len(csats)
        avg_csat = (sum(c.score for c in csats) / csat_count) if csat_count > 0 else 0.0
    else:
        csat_count = 0
        avg_csat = 0.0

    p1_count = sum(1 for t in tickets if t.priority == "P1")
    p2_count = sum(1 for t in tickets if t.priority == "P2")
    p3_count = sum(1 for t in tickets if t.priority == "P3")

    return {
        "start_time": start_time_utc.isoformat() if start_time_utc else None,
        "end_time": end_time_utc.isoformat() if end_time_utc else None,
        "total_tickets": total_tickets,
        "open_tickets": open_tickets,
        "pending_tickets": pending_tickets,
        "resolved_tickets": resolved_tickets,
        "average_first_response_time_seconds": round(avg_first_response, 2),
        "mean_time_to_resolution_seconds": round(mttr, 2),
        "total_sla_targets": total_sla_targets,
        "breached_sla_targets": breached_sla_targets,
        "sla_breach_rate_percent": round(breach_rate, 2),
        "average_csat_score": round(avg_csat, 2),
        "csat_response_count": csat_count,
        "priority_breakdown": {
            "P1": p1_count,
            "P2": p2_count,
            "P3": p3_count,
        },
    }


class TicketService:
    """Domain service encapsulating support ticket lifecycle and messaging operations [NFR-4]."""

    def __init__(
        self,
        session: Session,
        storage_port: Any | None = None,
    ) -> None:
        """Initialize TicketService with explicit dependency injection.

        Args:
            session: Active synchronous database session.
            storage_port: Optional storage connector/port instance.
        """
        self.session = session
        self.storage_port = storage_port

    def create_ticket(
        self,
        subject: str,
        description: str,
        client_id: str,
        priority: str = "P3",
        assigned_tier: str = "Tier 1",
        assigned_agent_id: str | None = None,
        created_at: datetime | None = None,
        initial_attachments: list[dict[str, Any]] | None = None,
    ) -> SupportTicket:
        """Create a new client support ticket."""
        return create_ticket(
            session=self.session,
            subject=subject,
            description=description,
            client_id=client_id,
            priority=priority,
            assigned_tier=assigned_tier,
            assigned_agent_id=assigned_agent_id,
            created_at=created_at,
            storage_port=self.storage_port,
            initial_attachments=initial_attachments,
        )

    def get_ticket(self, ticket_id: UUID | str) -> SupportTicket | None:
        """Retrieve a support ticket by ID."""
        return get_ticket(session=self.session, ticket_id=ticket_id)

    def list_tickets(
        self,
        client_id: str | None = None,
        priority: str | None = None,
        status: str | None = None,
        assigned_tier: str | None = None,
        assigned_agent_id: str | None = None,
        search_query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[SupportTicket], int]:
        """List support tickets matching filter criteria."""
        return list_tickets(
            session=self.session,
            client_id=client_id,
            priority=priority,
            status=status,
            assigned_tier=assigned_tier,
            assigned_agent_id=assigned_agent_id,
            search_query=search_query,
            limit=limit,
            offset=offset,
        )

    def update_ticket(
        self,
        ticket_id: UUID | str,
        status: str | None = None,
        priority: str | None = None,
        assigned_tier: str | None = None,
        assigned_agent_id: str | None = None,
        subject: str | None = None,
        description: str | None = None,
        updated_at: datetime | None = None,
    ) -> SupportTicket:
        """Update ticket status, priority, or assignments."""
        return update_ticket(
            session=self.session,
            ticket_id=ticket_id,
            status=status,
            priority=priority,
            assigned_tier=assigned_tier,
            assigned_agent_id=assigned_agent_id,
            subject=subject,
            description=description,
            updated_at=updated_at,
        )

    def delete_ticket(self, ticket_id: UUID | str) -> bool:
        """Delete a support ticket record."""
        return delete_ticket(session=self.session, ticket_id=ticket_id)

    def add_message(
        self,
        ticket_id: UUID | str,
        sender_id: str,
        sender_type: str,
        message_body: str,
        attachments: list[dict[str, Any]] | None = None,
        created_at: datetime | None = None,
    ) -> TicketMessage:
        """Append a message to a ticket thread."""
        return add_message(
            session=self.session,
            ticket_id=ticket_id,
            sender_id=sender_id,
            sender_type=sender_type,
            message_body=message_body,
            attachments=attachments,
            storage_port=self.storage_port,
            created_at=created_at,
        )

    def get_messages(self, ticket_id: UUID | str) -> list[TicketMessage]:
        """Retrieve thread message history for a ticket."""
        return get_ticket_messages(session=self.session, ticket_id=ticket_id)

    def submit_csat_rating(
        self,
        ticket_id: UUID | str,
        score: int,
        feedback_text: str | None = None,
        submitted_at: datetime | None = None,
    ) -> CSATRating:
        """Submit a CSAT rating for a resolved ticket."""
        return submit_csat_rating(
            session=self.session,
            ticket_id=ticket_id,
            score=score,
            feedback_text=feedback_text,
            submitted_at=submitted_at,
        )

    def get_csat_rating(self, ticket_id: UUID | str) -> CSATRating | None:
        """Retrieve CSAT rating submitted for a ticket."""
        return get_csat_rating(session=self.session, ticket_id=ticket_id)

    def get_analytics(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        priority: str | None = None,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate support metrics across a time window."""
        return get_support_analytics(
            session=self.session,
            start_time=start_time,
            end_time=end_time,
            priority=priority,
            client_id=client_id,
        )
