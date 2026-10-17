"""Unit tests for the calendar service module (FR-009, FR-010)."""

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.analytics import LeadEvent
from src.models.database import Base
from src.models.marketing import FollowupTask, LeadMeeting
from src.services.marketing.calendar import (
    InvalidCalendarDataError,
    MeetingNotFoundError,
    TaskNotFoundError,
    create_followup_task,
    create_lead_meeting,
    get_calendar_meetings,
    get_followup_tasks_for_lead,
    get_meeting_by_id,
    update_followup_task_status,
    update_meeting_notes,
)


@pytest.fixture
def test_db_session() -> Session:
    """Fixture providing an isolated in-memory SQLite database session."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(
        autocommit=False, autoflush=False, bind=engine
    )
    session = testing_session_local()
    try:
        yield session
    finally:
        session.close()


def test_create_lead_meeting(test_db_session: Session) -> None:
    """Test creating a lead meeting record."""
    lead_id = uuid.uuid4()
    scheduled_at = datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc)

    meeting_dto = create_lead_meeting(
        db=test_db_session,
        lead_id=lead_id,
        scheduled_time=scheduled_at,
        prep_notes="Prepare product demo deck.",
        meeting_notes=None,
    )

    assert isinstance(meeting_dto.id, uuid.UUID)
    assert meeting_dto.lead_id == lead_id
    assert meeting_dto.scheduled_time == scheduled_at
    assert meeting_dto.prep_notes == "Prepare product demo deck."
    assert meeting_dto.meeting_notes is None
    assert meeting_dto.followup_tasks == []
    assert meeting_dto.lead_status is None


def test_create_lead_meeting_invalid_id(test_db_session: Session) -> None:
    """Test creating a meeting with invalid lead_id raises InvalidCalendarDataError."""
    with pytest.raises(InvalidCalendarDataError):
        create_lead_meeting(
            db=test_db_session,
            lead_id="invalid-uuid",
            scheduled_time=datetime.now(timezone.utc),
        )


def test_get_meeting_by_id(test_db_session: Session) -> None:
    """Test fetching a meeting by ID."""
    lead_id = uuid.uuid4()
    scheduled_at = datetime(2026, 4, 2, 14, 30, tzinfo=timezone.utc)

    created = create_lead_meeting(
        db=test_db_session,
        lead_id=lead_id,
        scheduled_time=scheduled_at,
        prep_notes="Review pricing proposal.",
    )

    retrieved = get_meeting_by_id(test_db_session, created.id)
    assert retrieved.id == created.id
    assert retrieved.lead_id == lead_id
    assert retrieved.prep_notes == "Review pricing proposal."

    # Non-existent meeting
    with pytest.raises(MeetingNotFoundError):
        get_meeting_by_id(test_db_session, uuid.uuid4())


def test_get_calendar_meetings_filtering(test_db_session: Session) -> None:
    """Test querying meetings by date range, user assignment, and lead ID (FR-009)."""
    lead_1 = uuid.uuid4()
    lead_2 = uuid.uuid4()
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()

    now_utc = datetime.now(timezone.utc)
    t_past = now_utc - timedelta(days=2)
    t_today = now_utc
    t_future = now_utc + timedelta(days=5)

    m1 = create_lead_meeting(test_db_session, lead_1, t_past, prep_notes="Past meeting")
    m2 = create_lead_meeting(test_db_session, lead_1, t_today, prep_notes="Today meeting")
    m3 = create_lead_meeting(test_db_session, lead_2, t_future, prep_notes="Future meeting")

    # Add task assigned to user_a for m2
    create_followup_task(
        db=test_db_session,
        lead_id=lead_1,
        meeting_id=m2.id,
        task_description="Send follow-up email",
        assigned_user_id=user_a,
    )

    # Add task assigned to user_b for m3
    create_followup_task(
        db=test_db_session,
        lead_id=lead_2,
        meeting_id=m3.id,
        task_description="Prepare contract draft",
        assigned_user_id=user_b,
    )

    # 1. Query all meetings
    all_meetings = get_calendar_meetings(test_db_session)
    assert len(all_meetings) == 3

    # 2. Query date range
    window_meetings = get_calendar_meetings(
        test_db_session,
        start_date_val=t_today - timedelta(hours=1),
        end_date_val=t_future + timedelta(hours=1),
    )
    assert len(window_meetings) == 2
    meeting_ids = {m.id for m in window_meetings}
    assert m2.id in meeting_ids
    assert m3.id in meeting_ids

    # 3. Query by user_id
    user_a_meetings = get_calendar_meetings(test_db_session, user_id=user_a)
    assert len(user_a_meetings) == 1
    assert user_a_meetings[0].id == m2.id

    # 4. Query by lead_id
    lead_1_meetings = get_calendar_meetings(test_db_session, lead_id=lead_1)
    assert len(lead_1_meetings) == 2


def test_update_meeting_notes_and_status_and_tasks(test_db_session: Session) -> None:
    """Test updating meeting notes, lead status transition, and generating follow-up tasks (FR-010)."""
    lead_id = uuid.uuid4()
    user_id = uuid.uuid4()
    scheduled_at = datetime(2026, 4, 10, 11, 0, tzinfo=timezone.utc)

    meeting = create_lead_meeting(
        db=test_db_session,
        lead_id=lead_id,
        scheduled_time=scheduled_at,
        prep_notes="Initial discovery briefing.",
    )

    due_dt = datetime(2026, 4, 15, 17, 0, tzinfo=timezone.utc)
    task_payload = [
        {
            "task_description": "Send proposal PDF",
            "assigned_user_id": str(user_id),
            "due_date_val": due_dt.isoformat(),
        },
        {
            "description": "Schedule technical review",
            "assigned_user_id": user_id,
            "due_date_val": due_dt,
        },
    ]

    updated = update_meeting_notes(
        db=test_db_session,
        meeting_id=meeting.id,
        meeting_notes="Client confirmed technical requirement details.",
        lead_status="Qualified",
        tasks=task_payload,
    )

    assert updated.meeting_notes == "Client confirmed technical requirement details."
    assert updated.lead_status == "Qualified"
    assert len(updated.followup_tasks) == 2

    task_descriptions = {t.task_description for t in updated.followup_tasks}
    assert "Send proposal PDF" in task_descriptions
    assert "Schedule technical review" in task_descriptions

    # Verify LeadEvent record was created for lead_status transition
    lead_events = (
        test_db_session.query(LeadEvent)
        .filter(LeadEvent.lead_id == lead_id)
        .all()
    )
    assert len(lead_events) == 1
    assert lead_events[0].stage_name == "Qualified"


def test_update_meeting_notes_validation_and_errors(test_db_session: Session) -> None:
    """Test error handling in update_meeting_notes."""
    lead_id = uuid.uuid4()
    m = create_lead_meeting(
        db=test_db_session,
        lead_id=lead_id,
        scheduled_time=datetime.now(timezone.utc),
    )

    # Missing meeting error
    with pytest.raises(MeetingNotFoundError):
        update_meeting_notes(
            db=test_db_session,
            meeting_id=uuid.uuid4(),
            meeting_notes="Notes",
        )

    # Empty task description error
    with pytest.raises(InvalidCalendarDataError):
        update_meeting_notes(
            db=test_db_session,
            meeting_id=m.id,
            tasks=[{"task_description": ""}],
        )

    # Invalid due date string error
    with pytest.raises(InvalidCalendarDataError):
        update_meeting_notes(
            db=test_db_session,
            meeting_id=m.id,
            tasks=[
                {
                    "task_description": "Valid task",
                    "due_date_val": "not-a-date",
                }
            ],
        )


def test_standalone_followup_task_crud(test_db_session: Session) -> None:
    """Test direct creation, completion update, and query of follow-up tasks."""
    lead_id = uuid.uuid4()
    user_id = uuid.uuid4()

    # Create task
    task_dto = create_followup_task(
        db=test_db_session,
        lead_id=lead_id,
        task_description="Send NDA agreement",
        assigned_user_id=user_id,
        due_date_val=datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc),
    )

    assert task_dto.task_description == "Send NDA agreement"
    assert task_dto.is_completed is False
    assert task_dto.assigned_user_id == user_id

    # Update completion status
    updated_task = update_followup_task_status(
        db=test_db_session,
        task_id=task_dto.id,
        is_completed=True,
    )
    assert updated_task.is_completed is True

    # Retrieve tasks for lead
    lead_tasks = get_followup_tasks_for_lead(test_db_session, lead_id)
    assert len(lead_tasks) == 1
    assert lead_tasks[0].id == task_dto.id

    # Missing task error
    with pytest.raises(TaskNotFoundError):
        update_followup_task_status(
            db=test_db_session,
            task_id=uuid.uuid4(),
            is_completed=False,
        )
