"""FastAPI router and response schemas for marketing calendar and meeting prep tracking endpoints.

Provides GET /api/v1/marketing/calendar/meetings and POST /api/v1/marketing/calendar/meetings/{id}/notes
for calendar events, prep briefings, note logging, lead status updates, and task creation
with input sanitization and structured JSON logging with correlation IDs [FR-009, FR-010, NFR-003, NFR-004].
"""

from datetime import datetime
import html
import logging
import re
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from src.api.deps import CurrentUser, get_current_user
from src.lib.logging import get_correlation_id
from src.models.database import get_db
from src.services.marketing.calendar import (
    FollowupTaskDTO,
    InvalidCalendarDataError,
    MeetingCalendarDTO,
    MeetingNotFoundError,
    TaskNotFoundError,
    get_calendar_meetings,
    update_meeting_notes,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/marketing", tags=["marketing"])


def sanitize_input_string(v: str | None) -> str | None:
    """Sanitize and clean raw user string inputs.

    Strips leading/trailing whitespace, strips script/HTML markup tags,
    escapes special HTML characters, and removes unexpected control characters.
    """
    if v is None:
        return None

    cleaned = str(v).strip()
    if not cleaned:
        return cleaned

    # Remove script and HTML tags
    cleaned = re.sub(r"<[^>]*>", "", cleaned)
    # Escape special HTML characters
    cleaned = html.escape(cleaned)
    # Remove control characters except space, tab, newline
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", cleaned)
    return cleaned


class FollowupTaskCreate(BaseModel):
    """Pydantic schema for creating a follow-up task from meeting notes."""

    task_description: str = Field(..., description="Detailed description of the follow-up task")
    assigned_user_id: UUID | None = Field(default=None, description="Optional assigned user UUID")
    due_date_val: datetime | None = Field(default=None, description="Optional task due date timestamp (UTC)")
    is_completed: bool = Field(default=False, description="Task completion status")

    @field_validator("task_description", mode="before")
    @classmethod
    def sanitize_description(cls, v: Any) -> str:
        """Sanitize task description string."""
        if v is None:
            return ""
        sanitized = sanitize_input_string(str(v))
        return sanitized if sanitized is not None else ""

    @model_validator(mode="before")
    @classmethod
    def normalize_keys(cls, data: Any) -> Any:
        """Normalize task description and due date aliases if present."""
        if isinstance(data, dict):
            if "task_description" not in data and "description" in data:
                data["task_description"] = data["description"]
            if "due_date_val" not in data and "due_date" in data:
                data["due_date_val"] = data["due_date"]
        return data


class MeetingNotesRequest(BaseModel):
    """Pydantic request payload schema for logging meeting notes, updating lead status, and generating follow-up tasks."""

    meeting_notes: str | None = Field(default=None, description="Post-meeting summary notes")
    prep_notes: str | None = Field(default=None, description="Meeting preparation briefing notes")
    lead_status: str | None = Field(default=None, description="Target lead stage status name")
    tasks: list[FollowupTaskCreate] | None = Field(
        default=None, description="List of follow-up tasks to create"
    )

    @field_validator("meeting_notes", "prep_notes", "lead_status", mode="before")
    @classmethod
    def sanitize_optional_strings(cls, v: Any) -> str | None:
        """Sanitize optional string attributes."""
        if v is None:
            return None
        return sanitize_input_string(str(v))


class FollowupTaskResponse(BaseModel):
    """Pydantic response schema representing a follow-up task linked to a meeting or lead."""

    id: UUID = Field(..., description="Unique identifier of the follow-up task")
    meeting_id: UUID | None = Field(None, description="Optional linked meeting UUID")
    lead_id: UUID = Field(..., description="Unique identifier of the associated lead")
    assigned_user_id: UUID | None = Field(None, description="Optional assigned user UUID")
    due_date_val: datetime | None = Field(None, description="Task due date timestamp (UTC)")
    is_completed: bool = Field(..., description="Task completion status")
    task_description: str = Field(..., description="Detailed description of the task")
    created_on: datetime = Field(..., description="Task creation timestamp (UTC)")

    @field_validator("task_description", mode="before")
    @classmethod
    def sanitize_description(cls, v: Any) -> str:
        """Sanitize task description string."""
        if v is None:
            return ""
        sanitized = sanitize_input_string(str(v))
        return sanitized if sanitized is not None else ""

    @classmethod
    def from_dto(cls, dto: FollowupTaskDTO) -> "FollowupTaskResponse":
        """Convert a FollowupTaskDTO domain object into a response schema."""
        return cls(
            id=dto.id,
            meeting_id=dto.meeting_id,
            lead_id=dto.lead_id,
            assigned_user_id=dto.assigned_user_id,
            due_date_val=dto.due_date_val,
            is_completed=dto.is_completed,
            task_description=dto.task_description,
            created_on=dto.created_on,
        )


class MeetingCalendarResponse(BaseModel):
    """Pydantic response schema representing a calendar meeting entry with prep briefing and tasks."""

    id: UUID = Field(..., description="Unique identifier of the lead meeting")
    lead_id: UUID = Field(..., description="Unique identifier of the associated lead")
    scheduled_time: datetime = Field(..., description="Scheduled meeting time timestamp (UTC)")
    prep_notes: str | None = Field(None, description="Meeting preparation briefing notes")
    meeting_notes: str | None = Field(None, description="Post-meeting summary notes")
    created_on: datetime = Field(..., description="Meeting record creation timestamp (UTC)")
    followup_tasks: list[FollowupTaskResponse] = Field(
        default_factory=list,
        description="List of linked follow-up tasks",
    )
    lead_status: str | None = Field(None, description="Latest stage name status of the lead")

    @field_validator("prep_notes", "meeting_notes", "lead_status", mode="before")
    @classmethod
    def sanitize_optional_strings(cls, v: Any) -> str | None:
        """Sanitize optional string attributes."""
        if v is None:
            return None
        return sanitize_input_string(str(v))

    @classmethod
    def from_dto(cls, dto: MeetingCalendarDTO) -> "MeetingCalendarResponse":
        """Convert a MeetingCalendarDTO domain object into a response schema."""
        return cls(
            id=dto.id,
            lead_id=dto.lead_id,
            scheduled_time=dto.scheduled_time,
            prep_notes=dto.prep_notes,
            meeting_notes=dto.meeting_notes,
            created_on=dto.created_on,
            followup_tasks=[FollowupTaskResponse.from_dto(t) for t in dto.followup_tasks],
            lead_status=dto.lead_status,
        )


@router.get(
    "/calendar/meetings",
    response_model=list[MeetingCalendarResponse],
    status_code=status.HTTP_200_OK,
    summary="Retrieve calendar meetings, prep briefings, and linked tasks",
    description=(
        "Query upcoming and historical lead meetings, prep briefings, and linked follow-up tasks "
        "filtered by user assignment, date window, or lead ID [FR-009, NFR-003, NFR-004]."
    ),
)
def get_calendar_meetings_endpoint(
    user_id: str | None = Query(
        default=None,
        description="Optional filter by assigned user UUID.",
    ),
    start_date_val: datetime | None = Query(
        default=None,
        description="Filter meetings scheduled on or after this UTC timestamp (ISO 8601).",
    ),
    end_date_val: datetime | None = Query(
        default=None,
        description="Filter meetings scheduled on or before this UTC timestamp (ISO 8601).",
    ),
    lead_id: str | None = Query(
        default=None,
        description="Optional filter by lead UUID.",
    ),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> list[MeetingCalendarResponse]:
    """Retrieve calendar meetings, prep briefings, and linked tasks [FR-009, NFR-003, NFR-004]."""
    sanitized_user_id = sanitize_input_string(user_id)
    sanitized_lead_id = sanitize_input_string(lead_id)

    logger.info(
        "Querying marketing calendar meetings",
        extra={
            "correlation_id": get_correlation_id(),
            "user_id": str(current_user.user_id),
            "filter_user_id": sanitized_user_id,
            "filter_lead_id": sanitized_lead_id,
            "start_date_val": start_date_val.isoformat() if start_date_val else None,
            "end_date_val": end_date_val.isoformat() if end_date_val else None,
        },
    )

    try:
        meeting_dtos = get_calendar_meetings(
            db=db,
            user_id=sanitized_user_id,
            start_date_val=start_date_val,
            end_date_val=end_date_val,
            lead_id=sanitized_lead_id,
        )
        responses = [MeetingCalendarResponse.from_dto(dto) for dto in meeting_dtos]

        logger.info(
            f"Successfully retrieved {len(responses)} calendar meeting records",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "meeting_count": len(responses),
            },
        )
        return responses

    except InvalidCalendarDataError as exc:
        logger.warning(
            f"Invalid calendar meeting query parameters provided: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            f"Unexpected error retrieving calendar meetings: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "error": str(exc),
            },
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while retrieving calendar meetings.",
        ) from exc


@router.post(
    "/calendar/meetings/{id}/notes",
    response_model=MeetingCalendarResponse,
    status_code=status.HTTP_200_OK,
    summary="Log meeting notes, update lead status, and create follow-up tasks",
    description=(
        "Log meeting summary notes, update prep notes, trigger lead status transition via event progression, "
        "and generate follow-up tracking tasks with assigned due dates [FR-010, NFR-004]."
    ),
)
def update_meeting_notes_endpoint(
    id: UUID,
    payload: MeetingNotesRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> MeetingCalendarResponse:
    """Log meeting notes, update lead status, and create follow-up tasks [FR-010, NFR-004]."""
    logger.info(
        f"Logging notes and updating lead status for meeting '{id}'",
        extra={
            "correlation_id": get_correlation_id(),
            "user_id": str(current_user.user_id),
            "meeting_id": str(id),
            "has_meeting_notes": payload.meeting_notes is not None,
            "has_prep_notes": payload.prep_notes is not None,
            "lead_status": payload.lead_status,
            "task_count": len(payload.tasks) if payload.tasks else 0,
        },
    )

    tasks_data = (
        [t.model_dump() for t in payload.tasks] if payload.tasks is not None else None
    )

    try:
        updated_dto = update_meeting_notes(
            db=db,
            meeting_id=id,
            meeting_notes=payload.meeting_notes,
            prep_notes=payload.prep_notes,
            lead_status=payload.lead_status,
            tasks=tasks_data,
        )
        response = MeetingCalendarResponse.from_dto(updated_dto)

        logger.info(
            f"Successfully updated meeting notes for meeting '{id}'",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "meeting_id": str(id),
            },
        )
        return response

    except MeetingNotFoundError as exc:
        logger.warning(
            f"Meeting not found for notes update: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "meeting_id": str(id),
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except InvalidCalendarDataError as exc:
        logger.warning(
            f"Invalid data provided for meeting notes update: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "meeting_id": str(id),
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            f"Unexpected error updating meeting notes for meeting '{id}': {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "meeting_id": str(id),
                "error": str(exc),
            },
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while updating meeting notes.",
        ) from exc
