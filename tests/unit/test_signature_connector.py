"""Unit tests for NativeSignatureConnector and SignatureConnector [FR-5, FR-6]."""

from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.connectors.signature_connector import (
    NativeSignatureConnector,
    SignatureConnector,
)
from src.models.contract import Contract, SignatureRequest
from src.models.database import Base
from src.services.contract.models import (
    ContractCategory,
    ContractStatus,
    SignatureRequestDomain,
    SignatureStatus,
)


class MockNotificationPort:
    """Mock notification port to capture signature notification dispatches."""

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


def test_signature_connector_alias() -> None:
    """Verify SignatureConnector is an alias of NativeSignatureConnector."""
    assert SignatureConnector is NativeSignatureConnector


def test_in_memory_signature_connector_workflow() -> None:
    """Test full e-signature workflow in memory mode without database session [FR-5, FR-6]."""
    mock_notifier = MockNotificationPort()
    connector = NativeSignatureConnector(notification_port=mock_notifier)

    contract_id = uuid.uuid4()
    signatory_id_1 = uuid.uuid4()
    signatory_id_2 = uuid.uuid4()

    # 1. Create signature requests (FR-5)
    req1 = connector.create_signature_request(
        contract_id=contract_id, signatory_id=signatory_id_1
    )
    req2 = connector.create_signature_request(
        contract_id=contract_id, signatory_id=signatory_id_2
    )

    assert isinstance(req1, SignatureRequestDomain)
    assert req1.status == SignatureStatus.PENDING
    assert req1.contract_id == contract_id
    assert req1.signatory_id == signatory_id_1

    # 2. Get request by ID
    fetched = connector.get_signature_request(req1.id)
    assert fetched is not None
    assert fetched.id == req1.id

    # 3. Retrieve pending signatures for hub (FR-5)
    pending_all = connector.get_pending_signatures()
    assert len(pending_all) == 2

    pending_sig1 = connector.get_pending_signatures(signatory_id=signatory_id_1)
    assert len(pending_sig1) == 1
    assert pending_sig1[0].id == req1.id

    pending_contract = connector.get_pending_signatures(contract_id=contract_id)
    assert len(pending_contract) == 2

    # 4. Execute signature (FR-6)
    digest = "sha256_digest_12345"
    exec_result = connector.execute_signature(
        request_id=req1.id,
        signature_digest=digest,
        signatory_id=signatory_id_1,
    )

    assert exec_result.success is True
    assert exec_result.status == SignatureStatus.EXECUTED
    assert exec_result.signature_digest == digest
    assert exec_result.executed_at is not None

    # Verify notification was dispatched via notification port
    assert len(mock_notifier.notifications) == 1
    assert str(signatory_id_1) in mock_notifier.notifications[0]["recipient"]

    # Verify req1 is no longer pending
    pending_remaining = connector.get_pending_signatures()
    assert len(pending_remaining) == 1
    assert pending_remaining[0].id == req2.id

    # 5. Execute already executed request raises ValueError
    with pytest.raises(ValueError, match="is not in PENDING state"):
        connector.execute_signature(
            request_id=req1.id,
            signature_digest="another_digest",
        )

    # 6. Reject signature
    rejected = connector.reject_signature(
        request_id=req2.id,
        reason="Terms not acceptable",
        signatory_id=signatory_id_2,
    )
    assert rejected.status == SignatureStatus.REJECTED

    # 7. Cancel signature request
    unknown_id = uuid.uuid4()
    assert connector.cancel_signature_request(unknown_id) is False
    assert connector.cancel_signature_request(req1.id) is True


def test_database_backed_signature_connector() -> None:
    """Test NativeSignatureConnector using synchronous SQLAlchemy session with SQLite [FR-5, FR-6]."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    db: Session = TestingSessionLocal()

    try:
        # Seed contract in PENDING_SIGNATURE status
        contract_id = uuid.uuid4()
        contract = Contract(
            id=contract_id,
            title="Service Level Agreement",
            category=ContractCategory.CONTRACT.value,
            status=ContractStatus.PENDING_SIGNATURE.value,
            counterparty="Acme Corp",
        )
        db.add(contract)
        db.commit()

        signatory_id = uuid.uuid4()
        mock_notifier = MockNotificationPort()
        connector = NativeSignatureConnector(db=db, notification_port=mock_notifier)

        # 1. Create signature request
        req = connector.create_signature_request(
            contract_id=contract_id, signatory_id=signatory_id
        )
        assert req.status == SignatureStatus.PENDING

        # Verify DB record exists
        db_req = (
            db.query(SignatureRequest)
            .filter(SignatureRequest.id == req.id)
            .first()
        )
        assert db_req is not None
        assert db_req.status == "PENDING"
        assert db_req.contract_id == contract_id

        # 2. Query pending signatures for hub
        pending_list = connector.get_pending_signatures(contract_id=contract_id)
        assert len(pending_list) == 1
        assert pending_list[0].id == req.id

        # 3. Execute signature and verify contract status transitions to ACTIVE (FR-6)
        digest = "digest_xyz_789"
        result = connector.execute_signature(
            request_id=req.id,
            signature_digest=digest,
            signatory_id=signatory_id,
        )

        assert result.success is True
        assert result.status == SignatureStatus.EXECUTED

        # Verify DB request status
        db.refresh(db_req)
        assert db_req.status == "EXECUTED"
        assert db_req.signature_digest == digest

        # Verify Contract status transitioned to ACTIVE because no pending signatures remain (FR-6)
        db.refresh(contract)
        assert contract.status == ContractStatus.ACTIVE.value

        # 4. Create another request and reject it
        req2 = connector.create_signature_request(
            contract_id=contract_id, signatory_id=signatory_id
        )
        rejected = connector.reject_signature(request_id=req2.id, reason="Declined")
        assert rejected.status == SignatureStatus.REJECTED

        db_req2 = (
            db.query(SignatureRequest)
            .filter(SignatureRequest.id == req2.id)
            .first()
        )
        assert db_req2 is not None
        assert db_req2.status == "REJECTED"

        # 5. Cancel request
        assert connector.cancel_signature_request(req2.id) is True
        db_req2_deleted = (
            db.query(SignatureRequest)
            .filter(SignatureRequest.id == req2.id)
            .first()
        )
        assert db_req2_deleted is None

    finally:
        db.close()


def test_execute_signature_non_existent_request() -> None:
    """Verify executing non-existent request raises ValueError."""
    connector = NativeSignatureConnector()
    with pytest.raises(ValueError, match="not found"):
        connector.execute_signature(
            request_id=uuid.uuid4(),
            signature_digest="digest",
        )


def test_reject_signature_non_existent_request() -> None:
    """Verify rejecting non-existent request raises ValueError."""
    connector = NativeSignatureConnector()
    with pytest.raises(ValueError, match="not found"):
        connector.reject_signature(
            request_id=uuid.uuid4(),
            reason="not found test",
        )
