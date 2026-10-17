"""Calendar service module for meeting prep tracking, note updates, lead status transitions, and follow-up task generation.

Provides calendar queries for upcoming lead meetings and prep briefings, note logging,
lead status updates via event progression, and task assignment (FR-009, FR-010).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models.analytics import LeadEvent
from src.models.marketing import FollowupTask, LeadMeeting

logger = logging.getLogger(__name__)


class MeetingNotFoundError(ValueError):
    """Raised when a requested lead meeting record is not found in the database."""

    pass


class TaskNotFoundError(ValueError):
    """Raised when a requested follow-up task record is not found in the database."""

    pass


class InvalidCalendarDataError(ValueError):
    """Raised when calendar or task input parameters fail domain validation rules."""

    pass


@dataclass
class FollowupTaskDTO:
    """Data transfer object representing a follow-up task generated from meeting notes."""

    id: UUID
    meeting_id: UUID | None
    lead_id: UUID
    assigned_user_id: UUID | None
    due_date_val: datetime | None
    is_completed: bool
    task_description: str
    created_on: datetime


@dataclass
class MeetingCalendarDTO:
    """Data transfer object representing a calendar meeting entry with prep briefing and linked tasks."""

    id: UUID
    lead_id: UUID
    scheduled_time: datetime
    prep_notes: str | None
    meeting_notes: str | None
    created_on: datetime
    followup_tasks: list[FollowupTaskDTO] = field(default_factory=list)
    lead_status: str | None = None


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Ensure a datetime instance is timezone-aware in UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_uuid(val: UUID | str | None) -> UUID | None:
    """Parse a UUID or string into a UUID instance safely."""
    if val is None:
        return None
    if isinstance(val, UUID):
        return val
    try:
        return UUID(str(val))
    except (ValueError, AttributeError):
        raise InvalidCalendarDataError(f"Invalid UUID value format: {val!r}")


def _to_followup_task_dto(task: FollowupTask) -> FollowupTaskDTO:
    """Map a FollowupTask ORM instance to a FollowupTaskDTO."""
    return FollowupTaskDTO(
        id=task.id,
        meeting_id=task.meeting_id,
        lead_id=task.lead_id,
        assigned_user_id=task.assigned_user_id,
        due_date_val=_ensure_utc(task.due_date_val),
        is_completed=task.is_completed,
        task_description=task.task_description,
        created_on=_ensure_utc(task.created_on) or task.created_on,
    )


def _get_latest_lead_status(db: Session, lead_id: UUID) -> str | None:
    """Query the latest lead status (stage_name) from LeadEvent progression records."""
    stmt = (
        select(LeadEvent.stage_name)
        .where(LeadEvent.lead_id == lead_id)
        .order_by(LeadEvent.event_timestamp.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def _to_meeting_calendar_dto(db: Session, meeting: LeadMeeting) -> MeetingCalendarDTO:
    """Map a LeadMeeting ORM instance and its relationships to a MeetingCalendarDTO."""
    task_dtos = [_to_followup_task_dto(t) for t in meeting.followup_tasks]
    latest_status = _get_latest_lead_status(db, meeting.lead_id)

    return MeetingCalendarDTO(
        id=meeting.id,
        lead_id=meeting.lead_id,
        scheduled_time=_ensure_utc(meeting.scheduled_time) or meeting.scheduled_time,
        prep_notes=meeting.prep_notes,
        meeting_notes=meeting.meeting_notes,
        created_on=_ensure_utc(meeting.created_on) or meeting.created_on,
        followup_tasks=task_dtos,
        lead_status=latest_status,
    )


def create_lead_meeting(
    db: Session,
    lead_id: UUID | str,
    scheduled_time: datetime,
    prep_notes: str | None = None,
    meeting_notes: str | None = None,
) -> MeetingCalendarDTO:
    """Create a new lead meeting record for calendar preparation tracking."""
    lead_uuid = _parse_uuid(lead_id)
    if lead_uuid is None:
        raise InvalidCalendarDataError("Lead ID must not be None.")

    meeting = LeadMeeting(
        lead_id=lead_uuid,
        scheduled_time=_ensure_utc(scheduled_time) or scheduled_time,
        prep_notes=prep_notes,
        meeting_notes=meeting_notes,
    )
    db.add(meeting)
    db.commit()
    db.refresh(meeting)

    logger.info("Created LeadMeeting %s for lead %s", meeting.id, lead_uuid)
    return _to_meeting_calendar_dto(db, meeting)


def get_meeting_by_id(
    db: Session,
    meeting_id: UUID | str,
) -> MeetingCalendarDTO:
    """Retrieve a calendar meeting entry by ID."""
    meeting_uuid = _parse_uuid(meeting_id)
    if meeting_uuid is None:
        raise InvalidCalendarDataError("Meeting ID must not be None.")

    stmt = select(LeadMeeting).where(LeadMeeting.id == meeting_uuid)
    meeting = db.execute(stmt).scalar_one_or_none()
    if meeting is None:
        raise MeetingNotFoundError(f"Lead meeting with ID '{meeting_id}' not found.")

    return _to_meeting_calendar_dto(db, meeting)


def get_calendar_meetings(
    db: Session,
    user_id: UUID | str | None = None,
    start_date_val: datetime | None = None,
    end_date_val: datetime | None = None,
    lead_id: UUID | str | None = None,
) -> list[MeetingCalendarDTO]:
    """Query upcoming and historical calendar meetings filtered by optional user assignment and date window (FR-009)."""
    stmt = select(LeadMeeting)

    if start_date_val is not None:
        stmt = stmt.where(LeadMeeting.scheduled_time >= _ensure_utc(start_date_val))

    if end_date_val is not None:
        stmt = stmt.where(LeadMeeting.scheduled_time <= _ensure_utc(end_date_val))

    lead_uuid = _parse_uuid(lead_id) if lead_id is not None else None
    if lead_uuid is not None:
        stmt = stmt.where(LeadMeeting.lead_id == lead_uuid)

    user_uuid = _parse_uuid(user_id) if user_id is not None else None
    if user_uuid is not None:
        stmt = stmt.where(
            LeadMeeting.followup_tasks.any(FollowupTask.assigned_user_id == user_uuid)
        )

    stmt = stmt.order_by(LeadMeeting.scheduled_time.asc())
    meetings = db.execute(stmt).scalars().all()

    return [_to_meeting_calendar_dto(db, m) for m in meetings]


def update_meeting_notes(
    db: Session,
    meeting_id: UUID | str,
    meeting_notes: str | None = None,
    prep_notes: str | None = None,
    lead_status: str | None = None,
    tasks: list[dict[str, Any]] | None = None,
) -> MeetingCalendarDTO:
    """Log meeting notes, update lead status transition, and generate follow-up tracking tasks (FR-010)."""
    meeting_uuid = _parse_uuid(meeting_id)
    if meeting_uuid is None:
        raise InvalidCalendarDataError("Meeting ID must not be None.")

    stmt = select(LeadMeeting).where(LeadMeeting.id == meeting_uuid)
    meeting = db.execute(stmt).scalar_one_or_none()
    if meeting is None:
        raise MeetingNotFoundError(f"Lead meeting with ID '{meeting_id}' not found.")

    if meeting_notes is not None:
        meeting.meeting_notes = meeting_notes

    if prep_notes is not None:
        meeting.prep_notes = prep_notes

    if lead_status is not None and lead_status.strip():
        clean_status = lead_status.strip()
        lead_event = LeadEvent(
            lead_id=meeting.lead_id,
            stage_name=clean_status,
            event_timestamp=datetime.now(timezone.utc),
        )
        db.add(lead_event)
        logger.info(
            "Updated lead %s status to %r via meeting %s",
            meeting.lead_id,
            clean_status,
            meeting.id,
        )

    if tasks:
        for task_item in tasks:
            desc = task_item.get("task_description") or task_item.get("description")
            if not desc or not str(desc).strip():
                raise InvalidCalendarDataError(
                    "Follow-up task description must be a non-empty string."
                )

            assigned_id = task_item.get("assigned_user_id")
            assigned_uuid = _parse_uuid(assigned_id) if assigned_id else None

            due_val = task_item.get("due_date_val") or task_item.get("due_date")
            if isinstance(due_val, str):
                try:
                    due_val = datetime.fromisoformat(due_val)
                except ValueError:
                    raise InvalidCalendarDataError(f"Invalid due date format: {due_val!r}")

            task_obj = FollowupTask(
                meeting_id=meeting.id,
                lead_id=meeting.lead_id,
                assigned_user_id=assigned_uuid,
                due_date_val=_ensure_utc(due_val),
                task_description=str(desc).strip(),
                is_completed=bool(task_item.get("is_completed", False)),
            )
            db.add(task_obj)
            logger.info("Generated FollowupTask for meeting %s", meeting.id)

    db.commit()
    db.refresh(meeting)

    return _to_meeting_calendar_dto(db, meeting)


def create_followup_task(
    db: Session,
    lead_id: UUID | str,
    task_description: str,
    meeting_id: UUID | str | None = None,
    due_date_val: datetime | None = None,
    assigned_user_id: UUID | str | None = None,
    is_completed: bool = False,
) -> FollowupTaskDTO:
    """Create a follow-up task directly linked to a lead and optional meeting."""
    lead_uuid = _parse_uuid(lead_id)
    if lead_uuid is None:
        raise InvalidCalendarDataError("Lead ID must not be None.")

    if not task_description or not task_description.strip():
        raise InvalidCalendarDataError("Task description must be a non-empty string.")

    meeting_uuid = _parse_uuid(meeting_id) if meeting_id else None
    assigned_uuid = _parse_uuid(assigned_user_id) if assigned_user_id else None

    task = FollowupTask(
        meeting_id=meeting_uuid,
        lead_id=lead_uuid,
        assigned_user_id=assigned_uuid,
        due_date_val=_ensure_utc(due_date_val),
        is_completed=is_completed,
        task_description=task_description.strip(),
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    return _to_followup_task_dto(task)


def update_followup_task_status(
    db: Session,
    task_id: UUID | str,
    is_completed: bool,
) -> FollowupTaskDTO:
    """Update completion status for a follow-up task."""
    task_uuid = _parse_uuid(task_id)
    if task_uuid is None:
        raise InvalidCalendarDataError("Task ID must not be None.")

    stmt = select(FollowupTask).where(FollowupTask.id == task_uuid)
    task = db.execute(stmt).scalar_one_or_none()
    if task is None:
        raise TaskNotFoundError(f"Follow-up task with ID '{task_id}' not found.")

    task.is_completed = is_completed
    db.commit()
    db.refresh(task)

    return _to_followup_task_dto(task)


def get_followup_tasks_for_lead(
    db: Session,
    lead_id: UUID | str,
) -> list[FollowupTaskDTO]:
    """Retrieve all follow-up tasks linked to a specific lead."""
    lead_uuid = _parse_uuid(lead_id)
    if lead_uuid is None:
        raise InvalidCalendarDataError("Lead ID must not be None.")

    stmt = (
        select(FollowupTask)
        .where(FollowupTask.lead_id == lead_uuid)
        .order_by(FollowupTask.created_on.desc())
    )
    tasks = db.execute(stmt).scalars().all()

    return [_to_followup_task_dto(t) for t in tasks]
