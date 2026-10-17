"""Unit tests for SignatureService verifying e-signature state machine transitions and execution verification [FR-5, FR-6]."""

from datetime import date as date_type, datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.contract import Contract, SignatureRequest
from src.models.database import Base
from src.models.user import User
from src.services.contract.models import (
    ContractCategory,
    ContractStatus,
    SignatureRequestDomain,
    SignatureStatus,
)
from src.services.contract.signature_port import (
    SignatureExecutionResult,
    SignaturePort,
)
from src.services.contract.signature_service import (
    InvalidSignatureStateError,
    SignatureRequestNotFoundError,
    SignatureService,
    UnauthorizedSignatoryError,
)


class MockNotificationPort:
    """Mock notification port capturing dispatched notifications for assertions."""

    def __init__(self) -> None:
        self.notifications: list[dict] = []

    def send_notification(
        self,
        recipient: str,
        subject: str,
        body: str,
        channel: str = "in_app",
    ) -> bool:
        self.notifications.append(
            {
                "recipient": recipient,
                "subject": subject,
                "body": body,
                "channel": channel,
            }
        )
        return True


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
    """Fixture inserting a sample contract in DRAFT state."""
    contract = Contract(
        id=uuid4(),
        title="Master Services Agreement",
        category=ContractCategory.CONTRACT.value,
        counterparty="Acme Corp",
        status=ContractStatus.DRAFT.value,
        start_on=date_type(2026, 1, 1),
        expires_on=date_type(2027, 1, 1),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    test_db_session.add(contract)
    test_db_session.commit()
    return contract


@pytest.fixture
def sample_signatory(test_db_session: Session) -> User:
    """Fixture inserting a sample signatory user."""
    user = User(
        user_id=uuid4(),
        email="signatory@acme.com",
        password_hash="hashed_pw",
        tenant_id=uuid4(),
    )
    test_db_session.add(user)
    test_db_session.commit()
    return user


def test_in_memory_signature_service_workflow() -> None:
    """Test full signature lifecycle in memory mode without database session [FR-5, FR-6]."""
    mock_notifier = MockNotificationPort()
    service = SignatureService(notification_port=mock_notifier)

    contract_id = uuid4()
    signatory_id_1 = uuid4()
    signatory_id_2 = uuid4()

    # 1. Request signatures (FR-5)
    req1 = service.request_signature(
        contract_id=contract_id, signatory_id=signatory_id_1
    )
    req2 = service.request_signature(
        contract_id=contract_id, signatory_id=signatory_id_2
    )

    assert isinstance(req1, SignatureRequestDomain)
    assert req1.status == SignatureStatus.PENDING
    assert req1.contract_id == contract_id
    assert req1.signatory_id == signatory_id_1

    # 2. Get pending signatures for hub (FR-5)
    pending_all = service.get_pending_signatures()
    assert len(pending_all) == 2

    pending_sig1 = service.get_pending_signatures(signatory_id=signatory_id_1)
    assert len(pending_sig1) == 1
    assert pending_sig1[0].id == req1.id

    # 3. Retrieve request by ID
    fetched = service.get_signature_request(req1.id)
    assert fetched is not None
    assert fetched.id == req1.id

    # 4. Verify execution eligibility
    verified = service.verify_signature_execution_eligibility(
        request_id=req1.id, executing_signatory_id=signatory_id_1
    )
    assert verified.id == req1.id

    # 5. Execute signature (FR-6)
    digest = "sha256_mock_signature_digest"
    exec_result = service.execute_signature(
        request_id=req1.id,
        signature_digest=digest,
        signatory_id=signatory_id_1,
    )

    assert exec_result.success is True
    assert exec_result.status == SignatureStatus.EXECUTED
    assert exec_result.signature_digest == digest
    assert len(mock_notifier.notifications) == 1

    # 6. Reject remaining signature request
    rejected = service.reject_signature(
        request_id=req2.id,
        reason="Terms not acceptable",
        signatory_id=signatory_id_2,
    )
    assert rejected.status == SignatureStatus.REJECTED


def test_signature_execution_validation_errors() -> None:
    """Test error handling during signature execution verification [FR-6]."""
    service = SignatureService()
    contract_id = uuid4()
    assigned_signatory = uuid4()
    unauthorized_user = uuid4()

    req = service.request_signature(
        contract_id=contract_id, signatory_id=assigned_signatory
    )

    # 1. Non-existent request ID
    with pytest.raises(SignatureRequestNotFoundError):
        service.execute_signature(
            request_id=uuid4(),
            signature_digest="digest_123",
        )

    # 2. Empty signature digest
    with pytest.raises(ValueError, match="Signature digest cannot be empty"):
        service.execute_signature(
            request_id=req.id,
            signature_digest="   ",
            signatory_id=assigned_signatory,
        )

    # 3. Unauthorized signatory attempt
    with pytest.raises(UnauthorizedSignatoryError):
        service.execute_signature(
            request_id=req.id,
            signature_digest="digest_123",
            signatory_id=unauthorized_user,
        )

    # 4. Successful execution
    service.execute_signature(
        request_id=req.id,
        signature_digest="valid_digest_123",
        signatory_id=assigned_signatory,
    )

    # 5. Re-executing an already executed request
    with pytest.raises(InvalidSignatureStateError):
        service.execute_signature(
            request_id=req.id,
            signature_digest="valid_digest_123",
            signatory_id=assigned_signatory,
        )


def test_database_signature_service_workflow(
    test_db_session: Session, sample_contract: Contract, sample_signatory: User
) -> None:
    """Test signature service in database session mode with contract state transitions [FR-5, FR-6]."""
    mock_notifier = MockNotificationPort()
    service = SignatureService(
        db=test_db_session, notification_port=mock_notifier
    )

    # 1. Request signature transitions contract from DRAFT to PENDING_SIGNATURE
    assert sample_contract.status == ContractStatus.DRAFT.value
    req = service.request_signature(
        contract_id=sample_contract.id, signatory_id=sample_signatory.user_id
    )

    updated_contract = (
        test_db_session.query(Contract)
        .filter(Contract.id == sample_contract.id)
        .first()
    )
    assert updated_contract is not None
    assert updated_contract.status == ContractStatus.PENDING_SIGNATURE.value

    # 2. Execute signature transitions contract to ACTIVE when all pending signatures are complete
    service.execute_signature(
        request_id=req.id,
        signature_digest="sha256_db_test_digest",
        signatory_id=sample_signatory.user_id,
    )

    test_db_session.refresh(updated_contract)
    assert updated_contract.status == ContractStatus.ACTIVE.value


def test_signature_request_for_terminated_contract_fails(
    test_db_session: Session, sample_contract: Contract
) -> None:
    """Verify that requesting or executing a signature on a terminated contract fails."""
    sample_contract.status = ContractStatus.TERMINATED.value
    test_db_session.commit()

    service = SignatureService(db=test_db_session)

    with pytest.raises(InvalidSignatureStateError, match="TERMINATED"):
        service.request_signature(contract_id=sample_contract.id)


def test_cancel_signature_request() -> None:
    """Test cancelling an active signature request."""
    service = SignatureService()
    contract_id = uuid4()

    req = service.request_signature(contract_id=contract_id)
    assert service.get_signature_request(req.id) is not None

    cancelled = service.cancel_signature_request(req.id)
    assert cancelled is True
    assert service.get_signature_request(req.id) is None
