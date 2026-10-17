from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from src.models.database import Base
from src.models.marketing import FollowupTask, LeadMeeting, Territory


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


def test_marketing_models_tablenames() -> None:
    """Test that all marketing models have the expected table names."""
    assert Territory.__tablename__ == "territories"
    assert LeadMeeting.__tablename__ == "lead_meetings"
    assert FollowupTask.__tablename__ == "followup_tasks"


def test_territory_creation_and_unique_constraint(
    test_db_session: Session,
) -> None:
    """Test creation and unique constraint of Territory."""
    territory = Territory(
        territory_code="US-WEST",
        territory_name="US West Region",
        postal_code_patterns="900*, 901*, 902*",
    )
    test_db_session.add(territory)
    test_db_session.commit()
    test_db_session.refresh(territory)

    assert isinstance(territory.id, uuid.UUID)
    assert territory.territory_code == "US-WEST"
    assert territory.territory_name == "US West Region"
    assert territory.postal_code_patterns == "900*, 901*, 902*"
    assert isinstance(territory.created_on, datetime)
    assert "<Territory" in repr(territory)

    duplicate = Territory(
        territory_code="US-WEST",
        territory_name="Duplicate West",
    )
    test_db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()


def test_lead_meeting_creation(test_db_session: Session) -> None:
    """Test creation of LeadMeeting record."""
    lead_id = uuid.uuid4()
    scheduled_at = datetime(2026, 3, 15, 14, 0, 0, tzinfo=timezone.utc)

    meeting = LeadMeeting(
        lead_id=lead_id,
        scheduled_time=scheduled_at,
        prep_notes="Review Q1 product usage statistics.",
        meeting_notes="Client expressed interest in enterprise upgrade.",
    )
    test_db_session.add(meeting)
    test_db_session.commit()
    test_db_session.refresh(meeting)

    assert isinstance(meeting.id, uuid.UUID)
    assert meeting.lead_id == lead_id
    assert meeting.scheduled_time == scheduled_at
    assert meeting.prep_notes == "Review Q1 product usage statistics."
    assert meeting.meeting_notes == "Client expressed interest in enterprise upgrade."
    assert isinstance(meeting.created_on, datetime)
    assert "<LeadMeeting" in repr(meeting)


def test_followup_task_creation_and_relationship(
    test_db_session: Session,
) -> None:
    """Test creation of FollowupTask linked to a LeadMeeting and relationship wiring."""
    lead_id = uuid.uuid4()
    user_id = uuid.uuid4()
    scheduled_at = datetime(2026, 3, 20, 10, 0, 0, tzinfo=timezone.utc)

    meeting = LeadMeeting(
        lead_id=lead_id,
        scheduled_time=scheduled_at,
        prep_notes="Initial discovery call.",
    )
    test_db_session.add(meeting)
    test_db_session.commit()

    due_date = datetime(2026, 3, 22, 17, 0, 0, tzinfo=timezone.utc)
    task = FollowupTask(
        meeting_id=meeting.id,
        lead_id=lead_id,
        assigned_user_id=user_id,
        due_date_val=due_date,
        task_description="Send custom proposal with SLA terms.",
        is_completed=False,
    )
    test_db_session.add(task)
    test_db_session.commit()
    test_db_session.refresh(task)

    assert isinstance(task.id, uuid.UUID)
    assert task.meeting_id == meeting.id
    assert task.lead_id == lead_id
    assert task.assigned_user_id == user_id
    assert task.due_date_val == due_date
    assert task.is_completed is False
    assert task.task_description == "Send custom proposal with SLA terms."
    assert task.meeting is not None
    assert task.meeting.id == meeting.id
    assert len(meeting.followup_tasks) == 1
    assert meeting.followup_tasks[0].id == task.id
    assert "<FollowupTask" in repr(task)


def test_meeting_cascade_deletes_followup_tasks(
    test_db_session: Session,
) -> None:
    """Test that deleting a LeadMeeting cascades deletion to linked FollowupTasks."""
    lead_id = uuid.uuid4()
    meeting = LeadMeeting(
        lead_id=lead_id,
        scheduled_time=datetime.now(timezone.utc),
    )
    test_db_session.add(meeting)
    test_db_session.commit()

    task1 = FollowupTask(
        meeting_id=meeting.id,
        lead_id=lead_id,
        task_description="Task 1",
    )
    task2 = FollowupTask(
        meeting_id=meeting.id,
        lead_id=lead_id,
        task_description="Task 2",
    )
    test_db_session.add_all([task1, task2])
    test_db_session.commit()

    assert test_db_session.query(FollowupTask).count() == 2

    test_db_session.delete(meeting)
    test_db_session.commit()

    assert test_db_session.query(FollowupTask).count() == 0
