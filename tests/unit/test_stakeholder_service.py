"""Unit tests for Stakeholder Service, contact mapping, influence tracking, and chronological activity feeds."""

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.database import Base
from src.models.deal import Deal
from src.services.deals.service import create_deal
from src.services.stakeholders.service import (
    DealNotFoundError,
    InvalidStakeholderDataError,
    StakeholderActivityDomain,
    StakeholderDomain,
    StakeholderMapResultDTO,
    StakeholderNotFoundError,
    StakeholderService,
    add_stakeholder_activity,
    create_stakeholder,
    deactivate_stakeholder,
    delete_stakeholder,
    get_activities_for_deal,
    get_activities_for_stakeholder,
    get_stakeholder_by_id,
    get_stakeholder_map_for_deal,
    get_stakeholders_for_deal,
    sync_crm_stakeholders_and_activities,
    update_stakeholder,
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


@pytest.fixture
def sample_deal(test_db_session: Session) -> Deal:
    """Fixture creating a sample deal in the database."""
    return create_deal(
        session=test_db_session,
        title="Acme Global ERP Expansion",
        deal_value=250000.00,
        milestone_stage="Proposal",
    )


def test_create_stakeholder_success(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Test successful creation of a stakeholder with role and influence level."""
    stk = create_stakeholder(
        session=test_db_session,
        deal_id=sample_deal.id,
        full_name="Jane Doe",
        email="jane.doe@acme.com",
        org_role="Chief Technology Officer",
        influence_level="High",
    )

    assert isinstance(stk.id, uuid.UUID)
    assert stk.deal_id == sample_deal.id
    assert stk.full_name == "Jane Doe"
    assert stk.email == "jane.doe@acme.com"
    assert stk.org_role == "Chief Technology Officer"
    assert stk.influence_level == "High"
    assert stk.is_active is True


def test_create_stakeholder_influence_case_insensitivity(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Test influence level normalization to title case (e.g. 'medium' -> 'Medium')."""
    stk = create_stakeholder(
        session=test_db_session,
        deal_id=str(sample_deal.id),
        full_name="John Smith",
        email="john.smith@acme.com",
        org_role="VP Procurement",
        influence_level="medium",
    )

    assert stk.influence_level == "Medium"


def test_create_stakeholder_invalid_influence_level(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Test validation failure when providing an invalid influence level."""
    with pytest.raises(InvalidStakeholderDataError, match="Invalid influence level"):
        create_stakeholder(
            session=test_db_session,
            deal_id=sample_deal.id,
            full_name="Alice Johnson",
            email="alice@acme.com",
            org_role="Director",
            influence_level="Critical",
        )


def test_create_stakeholder_invalid_email(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Test validation failure when email format is invalid."""
    with pytest.raises(InvalidStakeholderDataError, match="Invalid email address format"):
        create_stakeholder(
            session=test_db_session,
            deal_id=sample_deal.id,
            full_name="Bob Lee",
            email="bob-invalid-email",
            org_role="Manager",
            influence_level="Low",
        )


def test_create_stakeholder_empty_name(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Test validation failure when full name is empty."""
    with pytest.raises(InvalidStakeholderDataError, match="full name cannot be empty"):
        create_stakeholder(
            session=test_db_session,
            deal_id=sample_deal.id,
            full_name="   ",
            email="valid@acme.com",
            org_role="Lead Architect",
            influence_level="Low",
        )


def test_create_stakeholder_deal_not_found(test_db_session: Session) -> None:
    """Test creation failure when target deal ID does not exist."""
    fake_deal_id = uuid.uuid4()
    with pytest.raises(DealNotFoundError, match="not found"):
        create_stakeholder(
            session=test_db_session,
            deal_id=fake_deal_id,
            full_name="Charlie Brown",
            email="charlie@peanuts.com",
            org_role="Consultant",
            influence_level="Low",
        )


def test_get_stakeholder_by_id(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Test retrieving a stakeholder by unique ID."""
    created = create_stakeholder(
        session=test_db_session,
        deal_id=sample_deal.id,
        full_name="David Miller",
        email="david@acme.com",
        org_role="Legal Counsel",
        influence_level="Medium",
    )

    retrieved = get_stakeholder_by_id(test_db_session, created.id)
    assert retrieved.id == created.id
    assert retrieved.full_name == "David Miller"


def test_get_stakeholder_by_id_not_found(test_db_session: Session) -> None:
    """Test retrieval failure for non-existent stakeholder ID."""
    with pytest.raises(StakeholderNotFoundError):
        get_stakeholder_by_id(test_db_session, uuid.uuid4())


def test_get_stakeholders_for_deal_filtering(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Test getting stakeholders for a deal including active filtering."""
    stk1 = create_stakeholder(
        session=test_db_session,
        deal_id=sample_deal.id,
        full_name="Contact One",
        email="c1@acme.com",
        org_role="Sponsor",
        influence_level="High",
        is_active=True,
    )
    stk2 = create_stakeholder(
        session=test_db_session,
        deal_id=sample_deal.id,
        full_name="Contact Two",
        email="c2@acme.com",
        org_role="Evaluator",
        influence_level="Low",
        is_active=False,
    )

    all_stks = get_stakeholders_for_deal(test_db_session, sample_deal.id, active_only=False)
    assert len(all_stks) == 2

    active_stks = get_stakeholders_for_deal(test_db_session, sample_deal.id, active_only=True)
    assert len(active_stks) == 1
    assert active_stks[0].id == stk1.id


def test_deactivate_stakeholder(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Test flagging stakeholder as inactive (soft contact deletion)."""
    stk = create_stakeholder(
        session=test_db_session,
        deal_id=sample_deal.id,
        full_name="Eva Green",
        email="eva@acme.com",
        org_role="VP Sales",
        influence_level="High",
    )
    assert stk.is_active is True

    deactivated = deactivate_stakeholder(test_db_session, stk.id)
    assert deactivated.is_active is False

    refetched = get_stakeholder_by_id(test_db_session, stk.id)
    assert refetched.is_active is False


def test_update_stakeholder_details(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Test updating stakeholder contact details, role, and influence level."""
    stk = create_stakeholder(
        session=test_db_session,
        deal_id=sample_deal.id,
        full_name="Frank Wright",
        email="frank@acme.com",
        org_role="Manager",
        influence_level="Low",
    )

    updated = update_stakeholder(
        session=test_db_session,
        stakeholder_id=stk.id,
        org_role="Head of Engineering",
        influence_level="High",
    )

    assert updated.org_role == "Head of Engineering"
    assert updated.influence_level == "High"


def test_delete_stakeholder_permanent(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Test permanently deleting a stakeholder record."""
    stk = create_stakeholder(
        session=test_db_session,
        deal_id=sample_deal.id,
        full_name="Grace Hopper",
        email="grace@acme.com",
        org_role="Advisor",
        influence_level="Low",
    )

    delete_stakeholder(test_db_session, stk.id)

    with pytest.raises(StakeholderNotFoundError):
        get_stakeholder_by_id(test_db_session, stk.id)


def test_add_stakeholder_activity_and_chronological_ordering(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Test logging stakeholder activities and verifying chronological feed ordering (newest first)."""
    stk = create_stakeholder(
        session=test_db_session,
        deal_id=sample_deal.id,
        full_name="Henry Ford",
        email="henry@acme.com",
        org_role="COO",
        influence_level="High",
    )

    now = datetime.now(timezone.utc)
    t1 = now - timedelta(days=3)
    t2 = now - timedelta(days=1)
    t3 = now

    act1 = add_stakeholder_activity(
        session=test_db_session,
        deal_id=sample_deal.id,
        stakeholder_id=stk.id,
        activity_type="Email",
        summary="Initial discovery call follow-up email",
        occurred_on=t1,
    )

    act2 = add_stakeholder_activity(
        session=test_db_session,
        deal_id=sample_deal.id,
        stakeholder_id=stk.id,
        activity_type="Meeting",
        summary="Technical architecture review meeting",
        occurred_on=t2,
    )

    act3 = add_stakeholder_activity(
        session=test_db_session,
        deal_id=sample_deal.id,
        stakeholder_id=stk.id,
        activity_type="Call",
        summary="Commercial terms pricing discussion",
        occurred_on=t3,
    )

    feed = get_activities_for_deal(test_db_session, sample_deal.id)
    assert len(feed) == 3
    # Check chronological descending order
    assert feed[0].id == act3.id
    assert feed[1].id == act2.id
    assert feed[2].id == act1.id
    assert feed[0].stakeholder_name == "Henry Ford"


def test_get_stakeholder_map_for_deal(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Test retrieving combined stakeholder map and activity feed for a deal."""
    stk = create_stakeholder(
        session=test_db_session,
        deal_id=sample_deal.id,
        full_name="Irene Adler",
        email="irene@acme.com",
        org_role="Chief Security Officer",
        influence_level="High",
    )

    add_stakeholder_activity(
        session=test_db_session,
        deal_id=sample_deal.id,
        stakeholder_id=stk.id,
        activity_type="Meeting",
        summary="Security compliance review",
    )

    result: StakeholderMapResultDTO = get_stakeholder_map_for_deal(
        test_db_session, sample_deal.id
    )

    assert result.deal_id == sample_deal.id
    assert result.total_stakeholders == 1
    assert result.total_activities == 1
    assert result.stakeholders[0].full_name == "Irene Adler"
    assert result.recent_activities[0].summary == "Security compliance review"


def test_sync_crm_stakeholders_and_activities(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Test synchronizing external CRM contacts, updating existing, and deactivating missing contacts."""
    stk_old = create_stakeholder(
        session=test_db_session,
        deal_id=sample_deal.id,
        full_name="Old Contact",
        email="old@acme.com",
        org_role="Former Lead",
        influence_level="Low",
    )

    crm_contacts = [
        {
            "full_name": "New Contact 1",
            "email": "new1@acme.com",
            "org_role": "Decision Maker",
            "influence_level": "High",
            "is_active": True,
        },
    ]

    crm_activities = [
        {
            "activity_type": "Email",
            "summary": "CRM Ingested Email Thread",
            "occurred_on": "2026-10-01T12:00:00Z",
        }
    ]

    map_result = sync_crm_stakeholders_and_activities(
        session=test_db_session,
        deal_id=sample_deal.id,
        crm_stakeholders=crm_contacts,
        crm_activities=crm_activities,
    )

    # Check that old contact missing from crm_contacts is soft-deactivated
    old_updated = get_stakeholder_by_id(test_db_session, stk_old.id)
    assert old_updated.is_active is False

    # Check new contact is created
    active_stks = get_stakeholders_for_deal(test_db_session, sample_deal.id, active_only=True)
    assert len(active_stks) == 1
    assert active_stks[0].email == "new1@acme.com"


def test_stakeholder_service_class_wrapper(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Test StakeholderService object methods."""
    srv = StakeholderService(test_db_session)
    stk = srv.create_stakeholder(
        deal_id=sample_deal.id,
        full_name="Jack Sparrow",
        email="jack@blackpearl.com",
        org_role="Captain",
        influence_level="High",
    )

    assert stk.full_name == "Jack Sparrow"
    retrieved = srv.get_stakeholder_by_id(stk.id)
    assert retrieved.id == stk.id

    stk_map = srv.get_stakeholder_map(sample_deal.id)
    assert stk_map.total_stakeholders == 1
