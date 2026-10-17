"""Unit tests for AmendmentService verifying immutable append-only audit trail operations [FR-7, FR-8, NFR-4]."""

from datetime import date as date_type, datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.contract import Contract, ContractAmendment
from src.models.database import Base
from src.models.user import User
from src.services.contract.amendment_service import (
    AmendmentService,
    ImmutableAuditTrailError,
    to_amendment_domain_entity,
)
from src.services.contract.models import ContractCategory, ContractStatus


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
def sample_contract(test_db_session: Session) -> Contract:
    """Fixture inserting a sample contract for testing amendments."""
    contract = Contract(
        id=uuid4(),
        title="Software License Agreement",
        category=ContractCategory.CONTRACT.value,
        counterparty="TechCorp Inc",
        status=ContractStatus.ACTIVE.value,
        start_on=date_type(2026, 1, 1),
        expires_on=date_type(2027, 1, 1),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    test_db_session.add(contract)
    test_db_session.flush()
    test_db_session.commit()
    return contract


@pytest.fixture
def sample_user(test_db_session: Session) -> User:
    """Fixture inserting a sample user as author of amendments."""
    user = User(
        user_id=uuid4(),
        email="legal.counsel@example.com",
        password_hash="hashed_secret",
        created_at=datetime.now(timezone.utc),
    )
    test_db_session.add(user)
    test_db_session.flush()
    test_db_session.commit()
    return user


def test_add_amendment_sequential_versioning(
    test_db_session: Session, sample_contract: Contract, sample_user: User
) -> None:
    """Test appending amendments assigns incremental sequence numbers and preserves history [FR-7]."""
    service = AmendmentService(db=test_db_session)

    a1 = service.add_amendment(
        contract_id=sample_contract.id,
        summary="Added SLA credit escalation terms for downtime exceeding 4 hours.",
        author_id=sample_user.user_id,
    )

    assert a1.sequence_number == 1
    assert a1.contract_id == sample_contract.id
    assert a1.author_id == sample_user.user_id
    assert "SLA credit" in a1.summary

    a2 = service.add_amendment(
        contract_id=sample_contract.id,
        summary="Updated data processing addendum (DPA) annex.",
        author_id=sample_user.user_id,
    )

    assert a2.sequence_number == 2
    assert a2.contract_id == sample_contract.id

    # Verify contract status transitioned to AMENDED
    updated_contract = test_db_session.query(Contract).filter(Contract.id == sample_contract.id).first()
    assert updated_contract is not None
    assert updated_contract.status == ContractStatus.AMENDED.value


def test_get_amendments_for_contract_ordered(
    test_db_session: Session, sample_contract: Contract, sample_user: User
) -> None:
    """Test reading amendment audit history returns items ordered chronologically [FR-7]."""
    service = AmendmentService(db=test_db_session)

    service.add_amendment(
        contract_id=sample_contract.id,
        summary="First amendment summary.",
        author_id=sample_user.user_id,
    )
    service.add_amendment(
        contract_id=sample_contract.id,
        summary="Second amendment summary.",
        author_id=sample_user.user_id,
    )

    amendments = service.get_amendments_for_contract(sample_contract.id)
    assert len(amendments) == 2
    assert amendments[0].sequence_number == 1
    assert amendments[0].summary == "First amendment summary."
    assert amendments[1].sequence_number == 2
    assert amendments[1].summary == "Second amendment summary."

    assert service.get_amendment_count(sample_contract.id) == 2


def test_get_amendment_by_id(
    test_db_session: Session, sample_contract: Contract, sample_user: User
) -> None:
    """Test fetching individual amendment audit entry by UUID."""
    service = AmendmentService(db=test_db_session)

    added = service.add_amendment(
        contract_id=sample_contract.id,
        summary="Single amendment lookup test.",
        author_id=sample_user.user_id,
    )

    fetched = service.get_amendment_by_id(added.id)
    assert fetched is not None
    assert fetched.id == added.id
    assert fetched.summary == "Single amendment lookup test."

    non_existent = service.get_amendment_by_id(uuid4())
    assert non_existent is None


def test_add_amendment_validation_errors(
    test_db_session: Session, sample_contract: Contract
) -> None:
    """Test validation when adding an amendment with empty summary or invalid contract ID."""
    service = AmendmentService(db=test_db_session)

    with pytest.raises(ValueError, match="Amendment summary cannot be empty."):
        service.add_amendment(
            contract_id=sample_contract.id,
            summary="   ",
        )

    with pytest.raises(ValueError, match="Contract with ID .* not found."):
        service.add_amendment(
            contract_id=uuid4(),
            summary="Valid summary for non-existent contract",
        )


def test_service_mutation_methods_rejected(
    test_db_session: Session, sample_contract: Contract
) -> None:
    """Test that AmendmentService methods update_amendment, delete_amendment, clear_amendments raise ImmutableAuditTrailError [FR-8, NFR-4]."""
    service = AmendmentService(db=test_db_session)

    added = service.add_amendment(
        contract_id=sample_contract.id,
        summary="Initial amendment entry.",
    )

    with pytest.raises(ImmutableAuditTrailError, match="immutable and cannot be updated"):
        service.update_amendment(added.id, summary="Attempted tamper")

    with pytest.raises(ImmutableAuditTrailError, match="immutable and cannot be deleted"):
        service.delete_amendment(added.id)

    with pytest.raises(ImmutableAuditTrailError, match="Bulk deletion or truncation"):
        service.clear_amendments(sample_contract.id)


def test_orm_update_and_delete_blocked_by_listeners(
    test_db_session: Session, sample_contract: Contract
) -> None:
    """Test that direct SQLAlchemy ORM update and delete operations raise ImmutableAuditTrailError via listeners [FR-8, NFR-4]."""
    service = AmendmentService(db=test_db_session)

    added = service.add_amendment(
        contract_id=sample_contract.id,
        summary="Original unalterable amendment.",
    )

    orm_record = test_db_session.query(ContractAmendment).filter(ContractAmendment.id == added.id).first()
    assert orm_record is not None

    # Attempt direct ORM update
    orm_record.summary = "Directly altered summary text"
    with pytest.raises(ImmutableAuditTrailError, match="immutable. Editing or updating"):
        test_db_session.flush()

    test_db_session.rollback()

    # Attempt direct ORM delete
    orm_record_fresh = test_db_session.query(ContractAmendment).filter(ContractAmendment.id == added.id).first()
    assert orm_record_fresh is not None
    test_db_session.delete(orm_record_fresh)
    with pytest.raises(ImmutableAuditTrailError, match="immutable. Deleting amendment audit entries"):
        test_db_session.flush()

    test_db_session.rollback()


def test_to_amendment_domain_entity_mapping(sample_contract: Contract) -> None:
    """Test mapping helper function from ORM model to domain entity."""
    orm_obj = ContractAmendment(
        id=uuid4(),
        contract_id=sample_contract.id,
        author_id=uuid4(),
        sequence_number=5,
        summary="Mapped amendment summary",
        created_at=datetime.now(timezone.utc),
    )
    domain_entity = to_amendment_domain_entity(orm_obj)

    assert domain_entity.id == orm_obj.id
    assert domain_entity.contract_id == orm_obj.contract_id
    assert domain_entity.sequence_number == 5
    assert domain_entity.summary == "Mapped amendment summary"
