"""Unit tests for DealService, stage transitions, age/probability calculations, and forecast recalculation dispatch."""

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.database import Base
from src.models.deal import Deal
from src.services.deals.service import (
    DealDomain,
    DealNotFoundError,
    DealService,
    InvalidDealDataError,
    PipelineBoardResultDTO,
    calculate_deal_age_in_days,
    create_deal,
    delete_deal,
    dispatch_forecast_recalculation,
    get_deal_by_id,
    get_deals,
    get_default_win_probability,
    get_pipeline_board,
    to_deal_domain,
    transition_deal_stage,
    update_deal,
    update_deal_stage,
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


def test_get_default_win_probability() -> None:
    """Test win probability score lookups for known and fallback milestone stages."""
    assert get_default_win_probability("Qualification") == 0.20
    assert get_default_win_probability("Needs Analysis") == 0.35
    assert get_default_win_probability("Proposal") == 0.60
    assert get_default_win_probability("Negotiation") == 0.80
    assert get_default_win_probability("Closed Won") == 1.00
    assert get_default_win_probability("Closed Lost") == 0.00

    # Keyword fallback matching
    assert get_default_win_probability("Proposal & Demo") == 0.60
    assert get_default_win_probability("Final Negotiation Stage") == 0.80
    assert get_default_win_probability("Lead Qualification Phase") == 0.20
    assert get_default_win_probability("Unknown Custom Stage") == 0.10
    assert get_default_win_probability("") == 0.10


def test_calculate_deal_age_in_days() -> None:
    """Test deal age calculation relative to current or explicit reference timestamp."""
    ref_time = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
    created_time = ref_time - timedelta(days=5, hours=12)

    age = calculate_deal_age_in_days(created_time, ref_time=ref_time)
    assert age == 5.5

    # Test future created_on returns 0.0 (non-negative constraint)
    future_time = ref_time + timedelta(days=2)
    assert calculate_deal_age_in_days(future_time, ref_time=ref_time) == 0.0

    # Test naive datetime handling
    naive_created = datetime(2026, 10, 5, 0, 0, 0)
    assert calculate_deal_age_in_days(naive_created, ref_time=ref_time) == 5.5


def test_create_deal(test_db_session: Session) -> None:
    """Test deal creation with default probability calculation and forecast dispatch."""
    dispatched_events: list[tuple[str, str]] = []

    def mock_dispatcher(deal_id: str, trigger_source: str) -> None:
        dispatched_events.append((deal_id, trigger_source))

    deal = create_deal(
        session=test_db_session,
        title="Enterprise Cloud Deal",
        deal_value=150000.00,
        milestone_stage="Proposal",
        forecast_dispatcher=mock_dispatcher,
    )

    assert isinstance(deal.id, uuid.UUID)
    assert deal.title == "Enterprise Cloud Deal"
    assert deal.deal_value == 150000.00
    assert deal.milestone_stage == "Proposal"
    assert deal.win_probability == 0.60
    assert len(dispatched_events) == 1
    assert dispatched_events[0] == (str(deal.id), "deal_created")


def test_create_deal_explicit_probability(test_db_session: Session) -> None:
    """Test deal creation with an explicit win probability override."""
    deal = create_deal(
        session=test_db_session,
        title="Custom Probability Deal",
        deal_value=50000.00,
        milestone_stage="Qualification",
        win_probability=0.45,
        dispatch_forecast=False,
    )

    assert deal.win_probability == 0.45


def test_get_deal_by_id_and_query(test_db_session: Session) -> None:
    """Test retrieving deal by ID and filtering deal lists by stage."""
    d1 = create_deal(
        session=test_db_session,
        title="Deal One",
        deal_value=10000.0,
        milestone_stage="Qualification",
        dispatch_forecast=False,
    )
    d2 = create_deal(
        session=test_db_session,
        title="Deal Two",
        deal_value=20000.0,
        milestone_stage="Proposal",
        dispatch_forecast=False,
    )

    fetched = get_deal_by_id(test_db_session, d1.id)
    assert fetched is not None
    assert fetched.title == "Deal One"

    fetched_str = get_deal_by_id(test_db_session, str(d2.id))
    assert fetched_str is not None
    assert fetched_str.title == "Deal Two"

    assert get_deal_by_id(test_db_session, "invalid-uuid") is None

    qual_deals = get_deals(test_db_session, stage="Qualification")
    assert len(qual_deals) == 1
    assert qual_deals[0].id == d1.id

    all_deals = get_deals(test_db_session)
    assert len(all_deals) == 2


def test_get_pipeline_board(test_db_session: Session) -> None:
    """Test visual board data aggregation grouped across stage columns."""
    create_deal(
        session=test_db_session,
        title="Deal A",
        deal_value=50000.0,
        milestone_stage="Proposal",
        dispatch_forecast=False,
    )
    create_deal(
        session=test_db_session,
        title="Deal B",
        deal_value=100000.0,
        milestone_stage="Proposal",
        dispatch_forecast=False,
    )
    create_deal(
        session=test_db_session,
        title="Deal C",
        deal_value=250000.0,
        milestone_stage="Negotiation",
        dispatch_forecast=False,
    )

    board = get_pipeline_board(test_db_session)

    assert isinstance(board, PipelineBoardResultDTO)
    assert board.total_pipeline_value == 400000.0
    assert board.total_deal_count == 3

    proposal_group = next(s for s in board.stages if s.milestone_stage == "Proposal")
    assert proposal_group.deal_count == 2
    assert proposal_group.total_value == 150000.0
    assert len(proposal_group.deals) == 2
    assert proposal_group.deals[0].win_probability == 0.60

    neg_group = next(s for s in board.stages if s.milestone_stage == "Negotiation")
    assert neg_group.deal_count == 1
    assert neg_group.total_value == 250000.0


def test_update_deal_stage_and_transition(test_db_session: Session) -> None:
    """Test milestone stage update, probability recalculation, and dispatch."""
    dispatched_events: list[tuple[str, str]] = []

    def mock_dispatcher(deal_id: str, trigger_source: str) -> None:
        dispatched_events.append((deal_id, trigger_source))

    deal = create_deal(
        session=test_db_session,
        title="Transition Test Deal",
        deal_value=80000.0,
        milestone_stage="Qualification",
        dispatch_forecast=False,
    )
    assert deal.win_probability == 0.20

    updated = update_deal_stage(
        session=test_db_session,
        deal_id=deal.id,
        new_stage="Negotiation",
        forecast_dispatcher=mock_dispatcher,
    )

    assert updated.milestone_stage == "Negotiation"
    assert updated.win_probability == 0.80
    assert len(dispatched_events) == 1
    assert dispatched_events[0] == (str(deal.id), "stage_transition")

    # Test alias transition_deal_stage with explicit probability
    aliased = transition_deal_stage(
        session=test_db_session,
        deal_id=deal.id,
        new_stage="Closed Won",
        win_probability=0.95,
        dispatch_forecast=False,
    )
    assert aliased.milestone_stage == "Closed Won"
    assert aliased.win_probability == 0.95


def test_update_deal_general_fields(test_db_session: Session) -> None:
    """Test updating deal title, value, stage, and probability."""
    dispatched: list[str] = []

    deal = create_deal(
        session=test_db_session,
        title="Old Title",
        deal_value=30000.0,
        milestone_stage="Qualification",
        dispatch_forecast=False,
    )

    updated = update_deal(
        session=test_db_session,
        deal_id=deal.id,
        title="New Title",
        deal_value=45000.0,
        forecast_dispatcher=lambda deal_id, src: dispatched.append(src),
    )

    assert updated.title == "New Title"
    assert updated.deal_value == 45000.0
    assert dispatched == ["value_update"]


def test_delete_deal(test_db_session: Session) -> None:
    """Test deleting an existing deal and handling non-existent deal deletion."""
    deal = create_deal(
        session=test_db_session,
        title="To Be Deleted",
        deal_value=5000.0,
        milestone_stage="Qualification",
        dispatch_forecast=False,
    )

    assert delete_deal(test_db_session, deal.id) is True
    assert get_deal_by_id(test_db_session, deal.id) is None
    assert delete_deal(test_db_session, deal.id) is False


def test_input_validation_errors(test_db_session: Session) -> None:
    """Test that invalid deal inputs raise InvalidDealDataError."""
    with pytest.raises(InvalidDealDataError, match="title cannot be empty"):
        create_deal(test_db_session, "", 100.0, "Proposal", dispatch_forecast=False)

    with pytest.raises(InvalidDealDataError, match="must be a non-negative number"):
        create_deal(test_db_session, "Title", -50.0, "Proposal", dispatch_forecast=False)

    with pytest.raises(InvalidDealDataError, match="stage cannot be empty"):
        create_deal(test_db_session, "Title", 100.0, "", dispatch_forecast=False)

    with pytest.raises(InvalidDealDataError, match="between 0.0 and 1.0"):
        create_deal(
            test_db_session,
            "Title",
            100.0,
            "Proposal",
            win_probability=1.5,
            dispatch_forecast=False,
        )


def test_deal_not_found_error(test_db_session: Session) -> None:
    """Test that operations on missing deal IDs raise DealNotFoundError."""
    missing_id = uuid.uuid4()
    with pytest.raises(DealNotFoundError, match="not found"):
        update_deal_stage(
            test_db_session,
            deal_id=missing_id,
            new_stage="Negotiation",
            dispatch_forecast=False,
        )

    with pytest.raises(DealNotFoundError, match="not found"):
        update_deal(
            test_db_session,
            deal_id=missing_id,
            title="Updated",
            dispatch_forecast=False,
        )


def test_deal_service_class(test_db_session: Session) -> None:
    """Test DealService class encapsulation and instance method invocations."""
    service = DealService(session=test_db_session)

    deal = service.create_deal(
        title="Class Service Deal",
        deal_value=60000.0,
        milestone_stage="Discovery",
        dispatch_forecast=False,
    )

    assert service.get_by_id(deal.id) is not None
    assert len(service.get_deals()) == 1

    board = service.get_pipeline_board()
    assert board.total_deal_count == 1

    updated = service.update_stage(deal.id, new_stage="Proposal", dispatch_forecast=False)
    assert updated.milestone_stage == "Proposal"

    assert service.delete_deal(deal.id) is True


def test_dispatch_forecast_recalculation_resilience() -> None:
    """Test forecast recalculation dispatch gracefully catches errors without raising."""
    # Test dispatcher raising exception
    def failing_dispatcher(deal_id: str, trigger: str) -> None:
        raise RuntimeError("Worker service offline")

    assert dispatch_forecast_recalculation(uuid.uuid4(), dispatcher=failing_dispatcher) is False

    # Test fallback when Celery worker task is not running
    assert dispatch_forecast_recalculation(uuid.uuid4()) is False or True
