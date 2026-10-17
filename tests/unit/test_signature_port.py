"""Unit tests for the SignaturePort abstract interface and SignatureExecutionResult DTO."""

from datetime import datetime, timezone
import uuid

import pytest

from src.services.contract.models import SignatureRequestDomain, SignatureStatus
from src.services.contract.signature_port import SignatureExecutionResult, SignaturePort


class DummySignatureConnector(SignaturePort):
    """Concrete dummy implementation of SignaturePort for unit testing."""

    def __init__(self) -> None:
        self.requests: dict[uuid.UUID, SignatureRequestDomain] = {}

    def create_signature_request(
        self,
        contract_id: uuid.UUID,
        signatory_id: uuid.UUID | None = None,
    ) -> SignatureRequestDomain:
        req_id = uuid.uuid4()
        req = SignatureRequestDomain(
            id=req_id,
            contract_id=contract_id,
            signatory_id=signatory_id,
            status=SignatureStatus.PENDING,
            signature_digest=None,
            executed_at=None,
            created_at=datetime.now(timezone.utc),
        )
        self.requests[req_id] = req
        return req

    def get_signature_request(self, request_id: uuid.UUID) -> SignatureRequestDomain | None:
        return self.requests.get(request_id)

    def get_pending_signatures(
        self,
        signatory_id: uuid.UUID | None = None,
        contract_id: uuid.UUID | None = None,
    ) -> list[SignatureRequestDomain]:
        results = [
            req for req in self.requests.values()
            if req.status == SignatureStatus.PENDING
        ]
        if signatory_id is not None:
            results = [req for req in results if req.signatory_id == signatory_id]
        if contract_id is not None:
            results = [req for req in results if req.contract_id == contract_id]
        return results

    def execute_signature(
        self,
        request_id: uuid.UUID,
        signature_digest: str,
        signatory_id: uuid.UUID | None = None,
        executed_at: datetime | None = None,
    ) -> SignatureExecutionResult:
        req = self.requests.get(request_id)
        if req is None:
            raise ValueError(f"Signature request {request_id} not found.")
        if req.status != SignatureStatus.PENDING:
            raise ValueError(f"Signature request {request_id} is not in PENDING state (current: {req.status}).")

        exec_time = executed_at or datetime.now(timezone.utc)
        req.status = SignatureStatus.EXECUTED
        req.signature_digest = signature_digest
        req.executed_at = exec_time
        if signatory_id is not None:
            req.signatory_id = signatory_id

        return SignatureExecutionResult(
            request_id=req.id,
            contract_id=req.contract_id,
            status=SignatureStatus.EXECUTED,
            signature_digest=signature_digest,
            executed_at=exec_time,
            signatory_id=req.signatory_id,
            success=True,
            message="Signature executed successfully",
        )

    def reject_signature(
        self,
        request_id: uuid.UUID,
        reason: str | None = None,
        signatory_id: uuid.UUID | None = None,
    ) -> SignatureRequestDomain:
        req = self.requests.get(request_id)
        if req is None:
            raise ValueError(f"Signature request {request_id} not found.")
        if req.status != SignatureStatus.PENDING:
            raise ValueError(f"Signature request {request_id} is not in PENDING state.")

        req.status = SignatureStatus.REJECTED
        if signatory_id is not None:
            req.signatory_id = signatory_id
        return req

    def cancel_signature_request(self, request_id: uuid.UUID) -> bool:
        if request_id in self.requests:
            del self.requests[request_id]
            return True
        return False


def test_cannot_instantiate_abstract_signature_port() -> None:
    """Verify that SignaturePort cannot be instantiated directly."""
    with pytest.raises(TypeError):
        SignaturePort()  # type: ignore[abstract]


def test_signature_execution_result_dto() -> None:
    """Verify SignatureExecutionResult DTO instantiation and attribute values."""
    req_id = uuid.uuid4()
    contract_id = uuid.uuid4()
    signatory_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    result = SignatureExecutionResult(
        request_id=req_id,
        contract_id=contract_id,
        status=SignatureStatus.EXECUTED,
        signature_digest="sha256_hash_digest_xyz",
        executed_at=now,
        signatory_id=signatory_id,
        success=True,
        message="Signature captured",
    )

    assert result.request_id == req_id
    assert result.contract_id == contract_id
    assert result.status == SignatureStatus.EXECUTED
    assert result.signature_digest == "sha256_hash_digest_xyz"
    assert result.executed_at == now
    assert result.signatory_id == signatory_id
    assert result.success is True
    assert result.message == "Signature captured"


def test_dummy_signature_connector_workflow() -> None:
    """Verify creating, querying pending, executing, and cancelling signature requests [FR-5, FR-6]."""
    connector = DummySignatureConnector()
    contract_id = uuid.uuid4()
    signatory_1 = uuid.uuid4()
    signatory_2 = uuid.uuid4()

    # Create two signature requests
    req1 = connector.create_signature_request(contract_id=contract_id, signatory_id=signatory_1)
    req2 = connector.create_signature_request(contract_id=contract_id, signatory_id=signatory_2)

    assert req1.status == SignatureStatus.PENDING
    assert req2.status == SignatureStatus.PENDING

    # Test get_signature_request
    fetched = connector.get_signature_request(req1.id)
    assert fetched is not None
    assert fetched.id == req1.id

    # Test get_pending_signatures (FR-5)
    pending_all = connector.get_pending_signatures()
    assert len(pending_all) == 2

    pending_sig1 = connector.get_pending_signatures(signatory_id=signatory_1)
    assert len(pending_sig1) == 1
    assert pending_sig1[0].id == req1.id

    # Test execute_signature (FR-6)
    digest = "digital_sig_digest_abc123"
    exec_result = connector.execute_signature(
        request_id=req1.id,
        signature_digest=digest,
        signatory_id=signatory_1,
    )

    assert exec_result.success is True
    assert exec_result.status == SignatureStatus.EXECUTED
    assert exec_result.signature_digest == digest
    assert exec_result.executed_at is not None

    # Verify req1 is no longer in pending list (FR-5)
    pending_after_exec = connector.get_pending_signatures()
    assert len(pending_after_exec) == 1
    assert pending_after_exec[0].id == req2.id

    # Verify executing already executed request raises ValueError
    with pytest.raises(ValueError, match="not in PENDING state"):
        connector.execute_signature(
            request_id=req1.id,
            signature_digest="another_digest",
        )

    # Test reject_signature
    rejected_req = connector.reject_signature(request_id=req2.id, reason="Declined to sign")
    assert rejected_req.status == SignatureStatus.REJECTED

    # Verify cancel_signature_request
    unknown_id = uuid.uuid4()
    assert connector.cancel_signature_request(unknown_id) is False
    assert connector.cancel_signature_request(req1.id) is True
