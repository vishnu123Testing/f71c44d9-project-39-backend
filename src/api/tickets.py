"""FastAPI router and Pydantic input validation schemas for support ticket CRUD, message posting, file attachments, and filtering REST endpoints.

Enforces strict boundary input sanitization, Pydantic validation, synchronous database persistence,
and structured context-correlated logging [FR-1, FR-2, FR-3, FR-7, NFR-2, NFR-3].
"""

import base64
import binascii
from datetime import datetime, timezone
import html
import logging
import re
from typing import Any
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from src.api.deps import CurrentUser, get_current_user
from src.connectors.storage import StorageConnector, StoragePort, sanitize_filename
from src.lib.logging import get_correlation_id
from src.models.database import get_db
from src.models.ticket import (
    CSATRating,
    SLATracking,
    SupportTicket,
    TicketAttachment,
    TicketMessage,
)
from src.services.support.ticket_service import (
    add_message,
    create_ticket,
    delete_ticket,
    get_csat_rating,
    get_ticket,
    get_ticket_messages,
    list_tickets,
    submit_csat_rating,
    update_ticket,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tickets"])


def get_storage_port() -> StoragePort:
    """Dependency provider for StoragePort connector instance."""
    return StorageConnector()


def sanitize_input_string(v: str | None) -> str | None:
    """Sanitize and clean raw user string inputs.

    Strips leading/trailing whitespace, strips script/HTML markup tags,
    escapes special characters, and removes unexpected control characters.
    """
    if v is None:
        return None

    cleaned = str(v).strip()
    if not cleaned:
        return cleaned

    # Remove HTML/script tags
    cleaned = re.sub(r"<[^>]*>", "", cleaned)
    # Escape remaining special HTML characters
    cleaned = html.escape(cleaned)
    # Remove control characters except tab, space, newline
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", cleaned)
    return cleaned


# ---------------------------------------------------------------------------
# Pydantic Schemas - Response Models
# ---------------------------------------------------------------------------


class AttachmentResponse(BaseModel):
    """Schema representing file attachment metadata in response payloads."""

    id: UUID = Field(..., description="Unique attachment UUID")
    message_id: UUID = Field(..., description="Associated message UUID")
    filename: str = Field(..., description="Original filename")
    file_size_bytes: int = Field(..., description="File payload size in bytes")
    mime_type: str = Field(..., description="MIME content type string")
    storage_key: str = Field(..., description="Storage key identifier")
    uploaded_at: datetime = Field(..., description="Upload UTC timestamp")

    @classmethod
    def from_orm_entity(cls, att: TicketAttachment) -> "AttachmentResponse":
        """Construct AttachmentResponse model from TicketAttachment ORM entity."""
        return cls(
            id=att.id,
            message_id=att.message_id,
            filename=att.filename,
            file_size_bytes=att.file_size_bytes,
            mime_type=att.mime_type,
            storage_key=att.storage_key,
            uploaded_at=att.uploaded_at,
        )


class MessageResponse(BaseModel):
    """Schema representing a ticket thread message in response payloads."""

    id: UUID = Field(..., description="Unique message UUID")
    ticket_id: UUID = Field(..., description="Associated ticket UUID")
    sender_id: str = Field(..., description="Sender ID string")
    sender_type: str = Field(..., description="Sender type: client, agent, system")
    message_body: str = Field(..., description="Message text body")
    created_at: datetime = Field(..., description="Creation UTC timestamp")
    attachments: list[AttachmentResponse] = Field(
        default_factory=list, description="Associated file attachments"
    )

    @classmethod
    def from_orm_entity(cls, msg: TicketMessage) -> "MessageResponse":
        """Construct MessageResponse model from TicketMessage ORM entity."""
        atts = getattr(msg, "attachments", None) or []
        return cls(
            id=msg.id,
            ticket_id=msg.ticket_id,
            sender_id=msg.sender_id,
            sender_type=msg.sender_type,
            message_body=msg.message_body,
            created_at=msg.created_at,
            attachments=[AttachmentResponse.from_orm_entity(a) for a in atts],
        )


class SLATrackingResponse(BaseModel):
    """Schema representing SLA target compliance status in response payloads."""

    id: UUID = Field(..., description="Unique SLA tracking UUID")
    ticket_id: UUID = Field(..., description="Associated ticket UUID")
    target_type: str = Field(..., description="Target type: first_response, resolution")
    target_due_at: datetime = Field(..., description="SLA target due UTC timestamp")
    warning_sent_at: datetime | None = Field(
        default=None, description="Warning dispatch UTC timestamp if sent"
    )
    breached_at: datetime | None = Field(
        default=None, description="Breach UTC timestamp if breached"
    )
    is_breached: bool = Field(..., description="SLA breach status flag")

    @classmethod
    def from_orm_entity(cls, tr: SLATracking) -> "SLATrackingResponse":
        """Construct SLATrackingResponse model from SLATracking ORM entity."""
        return cls(
            id=tr.id,
            ticket_id=tr.ticket_id,
            target_type=tr.target_type,
            target_due_at=tr.target_due_at,
            warning_sent_at=tr.warning_sent_at,
            breached_at=tr.breached_at,
            is_breached=tr.is_breached,
        )


class CSATRatingResponse(BaseModel):
    """Schema representing customer satisfaction survey feedback in response payloads."""

    id: UUID = Field(..., description="Unique CSAT rating UUID")
    ticket_id: UUID = Field(..., description="Associated ticket UUID")
    score: int = Field(..., description="CSAT score rating (1-5)")
    feedback_text: str | None = Field(
        default=None, description="Optional CSAT feedback text"
    )
    submitted_at: datetime = Field(..., description="Submission UTC timestamp")

    @classmethod
    def from_orm_entity(cls, csat: CSATRating) -> "CSATRatingResponse":
        """Construct CSATRatingResponse model from CSATRating ORM entity."""
        return cls(
            id=csat.id,
            ticket_id=csat.ticket_id,
            score=csat.score,
            feedback_text=csat.feedback_text,
            submitted_at=csat.submitted_at,
        )


class TicketSummaryResponse(BaseModel):
    """Schema representing ticket summary item in list response payloads."""

    id: UUID = Field(..., description="Unique ticket UUID")
    priority: str = Field(..., description="Priority code: P1, P2, P3")
    status: str = Field(..., description="Status code: open, pending, resolved, closed")
    subject: str = Field(..., description="Subject title string")
    description: str = Field(..., description="Detailed description text")
    client_id: str = Field(..., description="Client identifier string")
    assigned_tier: str = Field(..., description="Assigned support tier string")
    assigned_agent_id: str | None = Field(
        default=None, description="Assigned support agent ID string"
    )
    created_at: datetime = Field(..., description="Creation UTC timestamp")
    updated_at: datetime = Field(..., description="Last update UTC timestamp")
    first_responded_at: datetime | None = Field(
        default=None, description="First agent response UTC timestamp"
    )
    resolved_at: datetime | None = Field(
        default=None, description="Resolution UTC timestamp"
    )
    sla_trackings: list[SLATrackingResponse] = Field(
        default_factory=list, description="Associated SLA target tracking records"
    )

    @classmethod
    def from_orm_entity(cls, ticket: SupportTicket) -> "TicketSummaryResponse":
        """Construct TicketSummaryResponse model from SupportTicket ORM entity."""
        trackings = getattr(ticket, "sla_trackings", None) or []
        return cls(
            id=ticket.id,
            priority=ticket.priority,
            status=ticket.status,
            subject=ticket.subject,
            description=ticket.description,
            client_id=ticket.client_id,
            assigned_tier=ticket.assigned_tier,
            assigned_agent_id=ticket.assigned_agent_id,
            created_at=ticket.created_at,
            updated_at=ticket.updated_at,
            first_responded_at=ticket.first_responded_at,
            resolved_at=ticket.resolved_at,
            sla_trackings=[SLATrackingResponse.from_orm_entity(tr) for tr in trackings],
        )


class TicketDetailResponse(BaseModel):
    """Schema representing complete support ticket details in response payloads."""

    id: UUID = Field(..., description="Unique ticket UUID")
    priority: str = Field(..., description="Priority code: P1, P2, P3")
    status: str = Field(..., description="Status code: open, pending, resolved, closed")
    subject: str = Field(..., description="Subject title string")
    description: str = Field(..., description="Detailed description text")
    client_id: str = Field(..., description="Client identifier string")
    assigned_tier: str = Field(..., description="Assigned support tier string")
    assigned_agent_id: str | None = Field(
        default=None, description="Assigned support agent ID string"
    )
    created_at: datetime = Field(..., description="Creation UTC timestamp")
    updated_at: datetime = Field(..., description="Last update UTC timestamp")
    first_responded_at: datetime | None = Field(
        default=None, description="First agent response UTC timestamp"
    )
    resolved_at: datetime | None = Field(
        default=None, description="Resolution UTC timestamp"
    )
    messages: list[MessageResponse] = Field(
        default_factory=list, description="Sequential message thread history"
    )
    sla_trackings: list[SLATrackingResponse] = Field(
        default_factory=list, description="Associated SLA target tracking records"
    )
    csat_rating: CSATRatingResponse | None = Field(
        default=None, description="Submitted CSAT feedback rating if present"
    )

    @classmethod
    def from_orm_entity(cls, ticket: SupportTicket) -> "TicketDetailResponse":
        """Construct TicketDetailResponse model from SupportTicket ORM entity."""
        msgs = getattr(ticket, "messages", None) or []
        trackings = getattr(ticket, "sla_trackings", None) or []
        csat_obj = getattr(ticket, "csat_rating", None)
        csat_resp = (
            CSATRatingResponse.from_orm_entity(csat_obj)
            if csat_obj is not None
            else None
        )
        return cls(
            id=ticket.id,
            priority=ticket.priority,
            status=ticket.status,
            subject=ticket.subject,
            description=ticket.description,
            client_id=ticket.client_id,
            assigned_tier=ticket.assigned_tier,
            assigned_agent_id=ticket.assigned_agent_id,
            created_at=ticket.created_at,
            updated_at=ticket.updated_at,
            first_responded_at=ticket.first_responded_at,
            resolved_at=ticket.resolved_at,
            messages=[MessageResponse.from_orm_entity(m) for m in msgs],
            sla_trackings=[SLATrackingResponse.from_orm_entity(tr) for tr in trackings],
            csat_rating=csat_resp,
        )


class TicketListResponse(BaseModel):
    """Schema representing paginated list of support ticket summaries."""

    items: list[TicketSummaryResponse] = Field(
        default_factory=list, description="List of ticket summary records"
    )
    total_count: int = Field(..., description="Total count of matching tickets")
    limit: int = Field(..., description="Page item limit")
    offset: int = Field(..., description="Page offset integer")


# ---------------------------------------------------------------------------
# Pydantic Schemas - Request Models
# ---------------------------------------------------------------------------


class TicketCreateRequest(BaseModel):
    """Request payload schema for creating a new support ticket."""

    subject: str = Field(
        ..., min_length=1, max_length=255, description="Brief subject line"
    )
    description: str = Field(
        ..., min_length=1, description="Detailed description of issue"
    )
    client_id: str = Field(
        ..., min_length=1, max_length=255, description="Client/customer identifier"
    )
    priority: str = Field(
        default="P3", description="Priority level code: P1, P2, P3"
    )
    assigned_tier: str = Field(
        default="Tier 1", description="Support tier assignment string"
    )
    assigned_agent_id: str | None = Field(
        default=None, description="Optional assigned support agent ID"
    )

    @field_validator("subject", "client_id", "assigned_tier", "assigned_agent_id", mode="before")
    @classmethod
    def sanitize_string_fields(cls, v: Any) -> Any:
        """Sanitize string fields."""
        if v is None:
            return None
        sanitized = sanitize_input_string(str(v))
        return sanitized if sanitized is not None else ""

    @field_validator("description", mode="before")
    @classmethod
    def sanitize_description_field(cls, v: Any) -> Any:
        """Sanitize description body text."""
        if v is None:
            return ""
        sanitized = sanitize_input_string(str(v))
        return sanitized if sanitized is not None else ""

    @field_validator("subject", mode="after")
    @classmethod
    def validate_non_empty_subject(cls, v: str) -> str:
        """Ensure subject is non-empty after sanitization."""
        if not v or not v.strip():
            raise ValueError("Subject cannot be empty or whitespace only")
        return v.strip()

    @field_validator("description", mode="after")
    @classmethod
    def validate_non_empty_description(cls, v: str) -> str:
        """Ensure description is non-empty after sanitization."""
        if not v or not v.strip():
            raise ValueError("Description cannot be empty or whitespace only")
        return v.strip()

    @field_validator("priority", mode="before")
    @classmethod
    def validate_priority_code(cls, v: Any) -> str:
        """Validate and normalize priority code to P1, P2, or P3."""
        if v is None:
            return "P3"
        p = str(v).strip().upper()
        if p not in ("P1", "P2", "P3"):
            raise ValueError("Priority must be one of 'P1', 'P2', or 'P3'")
        return p


class TicketUpdateRequest(BaseModel):
    """Request payload schema for updating support ticket properties."""

    status: str | None = Field(
        default=None, description="Updated ticket status: open, pending, resolved, closed"
    )
    priority: str | None = Field(
        default=None, description="Updated priority code: P1, P2, P3"
    )
    assigned_tier: str | None = Field(
        default=None, description="Updated support tier assignment"
    )
    assigned_agent_id: str | None = Field(
        default=None, description="Updated assigned agent ID"
    )
    subject: str | None = Field(
        default=None, max_length=255, description="Updated subject title"
    )
    description: str | None = Field(
        default=None, description="Updated description text"
    )

    @field_validator("subject", "assigned_tier", "assigned_agent_id", mode="before")
    @classmethod
    def sanitize_string_fields(cls, v: Any) -> Any:
        """Sanitize string fields."""
        if v is None:
            return None
        return sanitize_input_string(str(v))

    @field_validator("description", mode="before")
    @classmethod
    def sanitize_description_field(cls, v: Any) -> Any:
        """Sanitize description text."""
        if v is None:
            return None
        return sanitize_input_string(str(v))

    @field_validator("priority", mode="before")
    @classmethod
    def validate_priority_code(cls, v: Any) -> str | None:
        """Validate and normalize priority code if provided."""
        if v is None:
            return None
        p = str(v).strip().upper()
        if p not in ("P1", "P2", "P3"):
            raise ValueError("Priority must be one of 'P1', 'P2', or 'P3'")
        return p

    @field_validator("status", mode="before")
    @classmethod
    def validate_status_code(cls, v: Any) -> str | None:
        """Validate and normalize status code if provided."""
        if v is None:
            return None
        s = str(v).strip().lower()
        if s not in ("open", "pending", "resolved", "closed"):
            raise ValueError("Status must be one of 'open', 'pending', 'resolved', or 'closed'")
        return s


class AttachmentCreateRequest(BaseModel):
    """Schema for inline file attachment data when posting a message."""

    filename: str = Field(..., description="Original filename")
    content: str = Field(..., description="Base64 encoded file payload string or raw text")
    mime_type: str | None = Field(default=None, description="Optional MIME content type string")

    @field_validator("filename", mode="before")
    @classmethod
    def sanitize_attachment_filename(cls, v: Any) -> Any:
        """Sanitize attachment filename."""
        if v is None:
            return "attachment.bin"
        return sanitize_filename(str(v))


class MessageCreateRequest(BaseModel):
    """Request payload schema for adding a message to a ticket thread."""

    message_body: str = Field(..., min_length=1, description="Message body text")
    sender_id: str | None = Field(
        default=None, description="Sender ID string (defaults to caller ID if omitted)"
    )
    sender_type: str = Field(
        default="client", description="Sender type code: client, agent, system"
    )
    attachments: list[AttachmentCreateRequest] | None = Field(
        default=None, description="Optional list of inline file attachments"
    )

    @field_validator("message_body", mode="before")
    @classmethod
    def sanitize_message_body(cls, v: Any) -> Any:
        """Sanitize message body text."""
        if v is None:
            return ""
        sanitized = sanitize_input_string(str(v))
        return sanitized if sanitized is not None else ""

    @field_validator("message_body", mode="after")
    @classmethod
    def validate_non_empty_body(cls, v: str) -> str:
        """Ensure message body is non-empty after sanitization."""
        if not v or not v.strip():
            raise ValueError("Message body cannot be empty or whitespace only")
        return v.strip()

    @field_validator("sender_id", "sender_type", mode="before")
    @classmethod
    def sanitize_string_fields(cls, v: Any) -> Any:
        """Sanitize string fields."""
        if v is None:
            return None
        return sanitize_input_string(str(v))

    @field_validator("sender_type", mode="after")
    @classmethod
    def validate_sender_type(cls, v: str | None) -> str:
        """Validate sender type code."""
        if not v:
            return "client"
        st = v.strip().lower()
        if st not in ("client", "agent", "system"):
            raise ValueError("sender_type must be one of 'client', 'agent', or 'system'")
        return st


class CSATRatingCreateRequest(BaseModel):
    """Request payload schema for submitting customer satisfaction (CSAT) rating feedback [FR-9]."""

    score: int = Field(..., ge=1, le=5, description="CSAT numerical rating score (1-5)")
    feedback_text: str | None = Field(
        default=None, description="Optional CSAT feedback commentary text"
    )

    @field_validator("feedback_text", mode="before")
    @classmethod
    def sanitize_feedback_text(cls, v: Any) -> Any:
        """Sanitize CSAT feedback commentary text input."""
        if v is None:
            return None
        return sanitize_input_string(str(v))

    @field_validator("score", mode="before")
    @classmethod
    def validate_score_range(cls, v: Any) -> int:
        """Validate CSAT score is an integer between 1 and 5."""
        if v is None:
            raise ValueError("CSAT score is required")
        try:
            val = int(v)
        except (ValueError, TypeError) as exc:
            raise ValueError("CSAT score must be an integer between 1 and 5") from exc
        if val < 1 or val > 5:
            raise ValueError("CSAT score must be an integer between 1 and 5")
        return val


# ---------------------------------------------------------------------------
# Route Handlers - Ticket CRUD & Filtering
# ---------------------------------------------------------------------------


@router.post(
    "/tickets",
    response_model=TicketDetailResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new support ticket",
    description=(
        "Create a new support ticket categorized by priority (P1, P2, P3), "
        "initialize SLA tracking targets, and create initial thread message (FR-1, FR-2, FR-7)."
    ),
)
def create_ticket_endpoint(
    payload: TicketCreateRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> TicketDetailResponse:
    """Create a new client support ticket and initialize SLA tracking."""
    try:
        ticket = create_ticket(
            session=db,
            subject=payload.subject,
            description=payload.description,
            client_id=payload.client_id,
            priority=payload.priority,
            assigned_tier=payload.assigned_tier,
            assigned_agent_id=payload.assigned_agent_id,
        )
        return TicketDetailResponse.from_orm_entity(ticket)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.error(
            f"Failed to create support ticket: {exc}",
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while creating the support ticket.",
        ) from exc


@router.get(
    "/tickets",
    response_model=TicketListResponse,
    status_code=status.HTTP_200_OK,
    summary="List and filter support tickets",
    description=(
        "Retrieve a paginated list of support tickets matching search and filter criteria "
        "including client_id, priority (P1, P2, P3), status, tier, assigned agent, or search query (FR-1)."
    ),
)
def list_tickets_endpoint(
    client_id: str | None = Query(default=None, description="Filter by client identifier"),
    priority: str | None = Query(default=None, description="Filter by priority (P1, P2, P3)"),
    status_filter: str | None = Query(
        default=None, alias="status", description="Filter by status (open, pending, resolved, closed)"
    ),
    assigned_tier: str | None = Query(default=None, description="Filter by assigned tier"),
    assigned_agent_id: str | None = Query(default=None, description="Filter by assigned agent ID"),
    search: str | None = Query(default=None, description="Search query string for subject or description"),
    limit: int = Query(default=50, ge=1, le=100, description="Page limit (1-100)"),
    offset: int = Query(default=0, ge=0, description="Page offset integer"),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> TicketListResponse:
    """Retrieve paginated list of support tickets matching filter parameters."""
    try:
        tickets, total_count = list_tickets(
            session=db,
            client_id=client_id,
            priority=priority,
            status=status_filter,
            assigned_tier=assigned_tier,
            assigned_agent_id=assigned_agent_id,
            search_query=search,
            limit=limit,
            offset=offset,
        )
        items = [TicketSummaryResponse.from_orm_entity(t) for t in tickets]
        return TicketListResponse(
            items=items,
            total_count=total_count,
            limit=limit,
            offset=offset,
        )
    except Exception as exc:
        logger.error(
            f"Failed to list support tickets: {exc}",
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while retrieving support tickets.",
        ) from exc


@router.get(
    "/tickets/{ticket_id}",
    response_model=TicketDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Get details for a specific support ticket",
    description="Retrieve full details for a support ticket by UUID including thread messages and active SLA countdown state (FR-1, FR-4).",
)
def get_ticket_endpoint(
    ticket_id: UUID,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> TicketDetailResponse:
    """Retrieve detailed support ticket record by UUID."""
    ticket = get_ticket(db, ticket_id)
    if not ticket:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ticket with ID '{ticket_id}' not found.",
        )
    return TicketDetailResponse.from_orm_entity(ticket)


@router.patch(
    "/tickets/{ticket_id}",
    response_model=TicketDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Update support ticket properties",
    description="Update support ticket status, priority, tier assignment, agent assignment, or content details (FR-1, FR-7).",
)
def update_ticket_endpoint(
    ticket_id: UUID,
    payload: TicketUpdateRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> TicketDetailResponse:
    """Update support ticket status, priority, or assignments."""
    try:
        ticket = update_ticket(
            session=db,
            ticket_id=ticket_id,
            status=payload.status,
            priority=payload.priority,
            assigned_tier=payload.assigned_tier,
            assigned_agent_id=payload.assigned_agent_id,
            subject=payload.subject,
            description=payload.description,
        )
        return TicketDetailResponse.from_orm_entity(ticket)
    except ValueError as exc:
        err_msg = str(exc)
        if "not found" in err_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=err_msg,
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=err_msg,
        ) from exc
    except Exception as exc:
        logger.error(
            f"Failed to update support ticket {ticket_id}: {exc}",
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while updating the support ticket.",
        ) from exc


@router.put(
    "/tickets/{ticket_id}",
    response_model=TicketDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Update support ticket properties (PUT alias)",
    description="Update support ticket status, priority, tier assignment, agent assignment, or content details (FR-1, FR-7).",
)
def put_update_ticket_endpoint(
    ticket_id: UUID,
    payload: TicketUpdateRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> TicketDetailResponse:
    """PUT method alias for updating support ticket details."""
    return update_ticket_endpoint(
        ticket_id=ticket_id,
        payload=payload,
        db=db,
        current_user=current_user,
    )


@router.delete(
    "/tickets/{ticket_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Delete a support ticket",
    description="Permanently delete a support ticket record and associated child entities. Returns HTTP 204 No Content.",
)
def delete_ticket_endpoint(
    ticket_id: UUID,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> Response:
    """Delete a support ticket record and associated child entities."""
    deleted = delete_ticket(db, ticket_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ticket with ID '{ticket_id}' not found.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Route Handlers - Message Posting & File Attachments
# ---------------------------------------------------------------------------


@router.post(
    "/tickets/{ticket_id}/messages",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a message to a support ticket thread",
    description=(
        "Append a new sequential message to a support ticket thread with optional "
        "file attachments and payload sanitization (FR-2, FR-3, FR-7, NFR-2)."
    ),
)
def add_message_endpoint(
    ticket_id: UUID,
    payload: MessageCreateRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    storage_port: StoragePort = Depends(get_storage_port),
) -> MessageResponse:
    """Post a message to a support ticket thread with optional inline attachments."""
    sender_id = payload.sender_id or current_user.email or str(current_user.user_id)

    attachments_data: list[dict[str, Any]] | None = None
    if payload.attachments:
        attachments_data = []
        for att in payload.attachments:
            content_bytes: bytes
            try:
                content_bytes = base64.b64decode(att.content, validate=True)
            except (binascii.Error, ValueError):
                content_bytes = att.content.encode("utf-8")

            attachments_data.append(
                {
                    "filename": att.filename,
                    "content": content_bytes,
                    "mime_type": att.mime_type or "application/octet-stream",
                }
            )

    try:
        msg = add_message(
            session=db,
            ticket_id=ticket_id,
            sender_id=sender_id,
            sender_type=payload.sender_type,
            message_body=payload.message_body,
            attachments=attachments_data,
            storage_port=storage_port,
        )
        return MessageResponse.from_orm_entity(msg)
    except ValueError as exc:
        err_msg = str(exc)
        if "not found" in err_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=err_msg,
            ) from exc
        if "validation failed" in err_msg.lower() or "attachment" in err_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=err_msg,
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=err_msg,
        ) from exc
    except Exception as exc:
        logger.error(
            f"Failed to add message to ticket {ticket_id}: {exc}",
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while adding message to ticket.",
        ) from exc


@router.get(
    "/tickets/{ticket_id}/messages",
    response_model=list[MessageResponse],
    status_code=status.HTTP_200_OK,
    summary="Get sequential message history for a ticket",
    description="Retrieve full sequential message thread history for a support ticket in chronological order (FR-2).",
)
def get_ticket_messages_endpoint(
    ticket_id: UUID,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> list[MessageResponse]:
    """Retrieve sequential message history for a ticket."""
    ticket = get_ticket(db, ticket_id)
    if not ticket:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ticket with ID '{ticket_id}' not found.",
        )
    messages = get_ticket_messages(db, ticket_id)
    return [MessageResponse.from_orm_entity(m) for m in messages]


@router.post(
    "/tickets/{ticket_id}/attachments",
    response_model=AttachmentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a file attachment for a ticket",
    description=(
        "Upload a binary file attachment for a support ticket, enforcing "
        "file size limits, extension, and MIME type validation (FR-3, NFR-2)."
    ),
)
def upload_attachment_endpoint(
    ticket_id: UUID,
    file: UploadFile = File(...),
    message_id: UUID | None = Form(default=None),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    storage_port: StoragePort = Depends(get_storage_port),
) -> AttachmentResponse:
    """Upload a file attachment bound to a ticket and message."""
    ticket = get_ticket(db, ticket_id)
    if not ticket:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ticket with ID '{ticket_id}' not found.",
        )

    messages = get_ticket_messages(db, ticket_id)
    target_message: TicketMessage | None = None

    if message_id is not None:
        for m in messages:
            if m.id == message_id:
                target_message = m
                break
        if target_message is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Message with ID '{message_id}' not found for ticket '{ticket_id}'.",
            )
    else:
        if messages:
            target_message = messages[-1]
        else:
            sender_id = current_user.email or str(current_user.user_id)
            target_message = add_message(
                session=db,
                ticket_id=ticket_id,
                sender_id=sender_id,
                sender_type="client",
                message_body="[File attachment uploaded]",
                storage_port=storage_port,
            )

    try:
        content = file.file.read()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to read file payload stream: {exc}",
        ) from exc

    raw_filename = file.filename or "attachment.bin"
    mime_type = file.content_type or "application/octet-stream"

    val_res = storage_port.validate_file(
        filename=raw_filename,
        file_size_bytes=len(content),
        mime_type=mime_type,
        content=content,
    )
    if not val_res.is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=val_res.error_message or "File attachment validation failed.",
        )

    try:
        stored = storage_port.store_file(
            filename=raw_filename,
            content=content,
            mime_type=mime_type,
        )
    except Exception as exc:
        logger.error(
            f"Failed to store file attachment for ticket {ticket_id}: {exc}",
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to store attachment payload: {exc}",
        ) from exc

    attachment_rec = TicketAttachment(
        id=uuid4(),
        message_id=target_message.id,
        filename=stored.filename,
        file_size_bytes=stored.file_size_bytes,
        mime_type=stored.mime_type,
        storage_key=stored.storage_key,
        uploaded_at=datetime.now(timezone.utc),
    )
    db.add(attachment_rec)
    db.flush()

    logger.info(
        f"File attachment uploaded for ticket {ticket_id}: {attachment_rec.id}",
        extra={"correlation_id": get_correlation_id()},
    )

    return AttachmentResponse.from_orm_entity(attachment_rec)


@router.post(
    "/tickets/{ticket_id}/messages/{message_id}/attachments",
    response_model=AttachmentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a file attachment for a specific message",
    description="Upload a binary file attachment bound to a specific ticket message (FR-3, NFR-2).",
)
def upload_message_attachment_endpoint(
    ticket_id: UUID,
    message_id: UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    storage_port: StoragePort = Depends(get_storage_port),
) -> AttachmentResponse:
    """Upload a file attachment bound to a specific message ID."""
    return upload_attachment_endpoint(
        ticket_id=ticket_id,
        file=file,
        message_id=message_id,
        db=db,
        current_user=current_user,
        storage_port=storage_port,
    )


@router.get(
    "/tickets/attachments/{attachment_id}",
    status_code=status.HTTP_200_OK,
    summary="Download/retrieve file attachment contents",
    description="Retrieve raw binary content for a file attachment by attachment UUID (FR-3).",
)
def download_attachment_endpoint(
    attachment_id: UUID,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    storage_port: StoragePort = Depends(get_storage_port),
) -> Response:
    """Retrieve raw file content for an attachment by ID."""
    attachment = db.get(TicketAttachment, attachment_id)
    if not attachment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Attachment with ID '{attachment_id}' not found.",
        )

    try:
        content = storage_port.retrieve_file(attachment.storage_key)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File content not found for attachment '{attachment_id}'.",
        ) from exc
    except Exception as exc:
        logger.error(
            f"Failed to retrieve attachment {attachment_id}: {exc}",
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve file attachment content.",
        ) from exc

    return Response(
        content=content,
        media_type=attachment.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{attachment.filename}"'
        },
    )


@router.get(
    "/tickets/{ticket_id}/attachments/{attachment_id}",
    status_code=status.HTTP_200_OK,
    summary="Download/retrieve file attachment contents for ticket",
    description="Retrieve raw binary content for a file attachment by ticket ID and attachment UUID (FR-3).",
)
def download_ticket_attachment_endpoint(
    ticket_id: UUID,
    attachment_id: UUID,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    storage_port: StoragePort = Depends(get_storage_port),
) -> Response:
    """Retrieve raw file content for an attachment by ticket ID and attachment ID."""
    ticket = get_ticket(db, ticket_id)
    if not ticket:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ticket with ID '{ticket_id}' not found.",
        )
    return download_attachment_endpoint(
        attachment_id=attachment_id,
        db=db,
        current_user=current_user,
        storage_port=storage_port,
    )


# ---------------------------------------------------------------------------
# Route Handlers - Customer Satisfaction (CSAT) Ratings
# ---------------------------------------------------------------------------


@router.post(
    "/tickets/{ticket_id}/csat",
    response_model=CSATRatingResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit CSAT rating for a resolved ticket",
    description=(
        "Submit customer satisfaction (CSAT) rating score (1-5) and optional "
        "feedback text for a resolved support ticket (FR-9)."
    ),
)
def submit_csat_rating_endpoint(
    ticket_id: UUID,
    payload: CSATRatingCreateRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> CSATRatingResponse:
    """Submit CSAT rating score and feedback commentary for a resolved ticket [FR-9]."""
    try:
        csat = submit_csat_rating(
            session=db,
            ticket_id=ticket_id,
            score=payload.score,
            feedback_text=payload.feedback_text,
        )
        return CSATRatingResponse.from_orm_entity(csat)
    except ValueError as exc:
        err_msg = str(exc)
        if "not found" in err_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=err_msg,
            ) from exc
        if "between 1 and 5" in err_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=err_msg,
            ) from exc
        if "already been submitted" in err_msg.lower() or "resolved or closed" in err_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=err_msg,
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=err_msg,
        ) from exc
    except Exception as exc:
        logger.error(
            f"Failed to submit CSAT rating for ticket {ticket_id}: {exc}",
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while submitting CSAT rating.",
        ) from exc


@router.get(
    "/tickets/{ticket_id}/csat",
    response_model=CSATRatingResponse,
    status_code=status.HTTP_200_OK,
    summary="Get CSAT rating for a ticket",
    description="Retrieve submitted CSAT rating feedback for a support ticket (FR-9).",
)
def get_csat_rating_endpoint(
    ticket_id: UUID,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> CSATRatingResponse:
    """Retrieve submitted CSAT rating feedback for a support ticket [FR-9]."""
    ticket = get_ticket(db, ticket_id)
    if not ticket:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ticket with ID '{ticket_id}' not found.",
        )
    csat = get_csat_rating(db, ticket_id)
    if not csat:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No CSAT rating found for ticket '{ticket_id}'.",
        )
    return CSATRatingResponse.from_orm_entity(csat)
