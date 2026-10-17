"""Native e-signature connector implementing SignaturePort for digital signature execution.

This module provides the concrete NativeSignatureConnector implementation for managing signature requests,
retrieving pending signatures for the central e-signature hub [FR-5], capturing digital signature
executions, transitioning contract status, and notifying relevant parties [FR-6].

Supports synchronous database persistence via SQLAlchemy Session when provided, and
falls back to in-memory state tracking when initialized without a database session.
"""

from datetime import datetime, timezone
import logging
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from src.lib.logging import get_correlation_id
from src.models.contract import Contract, SignatureRequest
from src.services.contract.models import (
    ContractStatus,
    SignatureRequestDomain,
    SignatureStatus,
)
from src.services.contract.signature_port import (
    SignatureExecutionResult,
    SignaturePort,
)

logger = logging.getLogger(__name__)


class NativeSignatureConnector(SignaturePort):
    """Native e-signature connector implementing SignaturePort for digital signature execution [FR-5, FR-6]."""

    def __init__(
        self,
        db: Session | None = None,
        notification_port: Any | None = None,
    ) -> None:
        """Initialize NativeSignatureConnector adapter.

        Args:
            db: Optional SQLAlchemy Session for database persistence.
            notification_port: Optional NotificationPort instance for dispatching signature notifications.
        """
        self.db = db
        self.notification_port = notification_port
        self._in_memory_requests: dict[UUID, SignatureRequestDomain] = {}

    def _to_domain(self, orm_req: SignatureRequest) -> SignatureRequestDomain:
        """Convert a SignatureRequest ORM model instance into a SignatureRequestDomain aggregate."""
        status_val: SignatureStatus | str = orm_req.status
        if isinstance(status_val, str):
            try:
                status_val = SignatureStatus(status_val)
            except ValueError:
                status_val = status_val

        return SignatureRequestDomain(
            id=orm_req.id,
            contract_id=orm_req.contract_id,
            signatory_id=orm_req.signatory_id,
            status=status_val,
            signature_digest=orm_req.signature_digest,
            executed_at=orm_req.executed_at,
            created_at=orm_req.created_at,
        )

    def create_signature_request(
        self,
        contract_id: UUID,
        signatory_id: UUID | None = None,
    ) -> SignatureRequestDomain:
        """Initiate and store a new e-signature request for a contract [FR-5].

        Args:
            contract_id: Unique UUID identifier of the contract.
            signatory_id: Optional UUID identifier of the assigned signatory.

        Returns:
            SignatureRequestDomain entity with PENDING status.
        """
        req_id = uuid4()
        now_utc = datetime.now(timezone.utc)

        domain_req = SignatureRequestDomain(
            id=req_id,
            contract_id=contract_id,
            signatory_id=signatory_id,
            status=SignatureStatus.PENDING,
            signature_digest=None,
            executed_at=None,
            created_at=now_utc,
        )

        if self.db is not None:
            orm_req = SignatureRequest(
                id=req_id,
                contract_id=contract_id,
                signatory_id=signatory_id,
                status=SignatureStatus.PENDING.value,
                signature_digest=None,
                executed_at=None,
                created_at=now_utc,
            )
            self.db.add(orm_req)
            self.db.commit()
            self.db.refresh(orm_req)
            domain_req = self._to_domain(orm_req)

        self._in_memory_requests[req_id] = domain_req

        logger.info(
            "Created new e-signature request for contract",
            extra={
                "correlation_id": get_correlation_id(),
                "request_id": str(req_id),
                "contract_id": str(contract_id),
                "signatory_id": str(signatory_id) if signatory_id else None,
                "status": SignatureStatus.PENDING.value,
            },
        )

        return domain_req

    def get_signature_request(self, request_id: UUID) -> SignatureRequestDomain | None:
        """Retrieve an e-signature request by its unique UUID identifier.

        Args:
            request_id: Unique UUID identifier of the signature request.

        Returns:
            SignatureRequestDomain entity if found, None otherwise.
        """
        if self.db is not None:
            orm_req = (
                self.db.query(SignatureRequest)
                .filter(SignatureRequest.id == request_id)
                .first()
            )
            if orm_req is not None:
                return self._to_domain(orm_req)

        return self._in_memory_requests.get(request_id)

    def get_pending_signatures(
        self,
        signatory_id: UUID | None = None,
        contract_id: UUID | None = None,
    ) -> list[SignatureRequestDomain]:
        """Retrieve all e-signature requests currently in PENDING status for the signature hub [FR-5].

        Args:
            signatory_id: Optional UUID identifier to filter pending requests for a specific signatory.
            contract_id: Optional UUID identifier to filter pending requests for a specific contract.

        Returns:
            List of pending SignatureRequestDomain entities.
        """
        results: list[SignatureRequestDomain] = []

        if self.db is not None:
            query = self.db.query(SignatureRequest).filter(
                SignatureRequest.status == SignatureStatus.PENDING.value
            )
            if signatory_id is not None:
                query = query.filter(SignatureRequest.signatory_id == signatory_id)
            if contract_id is not None:
                query = query.filter(SignatureRequest.contract_id == contract_id)

            orm_results = query.all()
            results = [self._to_domain(r) for r in orm_results]
        else:
            for req in self.list_all_requests():
                status_str = (
                    req.status.value
                    if isinstance(req.status, SignatureStatus)
                    else str(req.status)
                )
                if status_str != SignatureStatus.PENDING.value:
                    continue
                if signatory_id is not None and req.signatory_id != signatory_id:
                    continue
                if contract_id is not None and req.contract_id != contract_id:
                    continue
                results.append(req)

        return results

    def list_all_requests(self) -> list[SignatureRequestDomain]:
        """Retrieve all signature requests across all statuses."""
        if self.db is not None:
            orm_results = self.db.query(SignatureRequest).all()
            return [self._to_domain(r) for r in orm_results]
        return list(self._in_memory_requests.values())

    def execute_signature(
        self,
        request_id: UUID,
        signature_digest: str,
        signatory_id: UUID | None = None,
        executed_at: datetime | None = None,
    ) -> SignatureExecutionResult:
        """Capture digital signature execution and transition request status to EXECUTED [FR-6].

        Args:
            request_id: Unique UUID identifier of the signature request.
            signature_digest: Cryptographic hash or digital signature payload digest.
            signatory_id: Optional UUID identifier of the executing signatory for verification.
            executed_at: Optional explicit execution timestamp (defaults to current UTC time).

        Returns:
            SignatureExecutionResult representing the outcome of signature capture.

        Raises:
            ValueError: If the signature request is not found or not in PENDING status.
        """
        existing = self.get_signature_request(request_id)
        if existing is None:
            raise ValueError(f"Signature request '{request_id}' not found.")

        current_status = (
            existing.status.value
            if isinstance(existing.status, SignatureStatus)
            else str(existing.status)
        )
        if current_status != SignatureStatus.PENDING.value:
            raise ValueError(
                f"Signature request '{request_id}' is not in PENDING state (current status: '{current_status}')."
            )

        exec_time = executed_at or datetime.now(timezone.utc)
        effective_signatory_id = signatory_id or existing.signatory_id

        # Update DB persistence if session available
        if self.db is not None:
            orm_req = (
                self.db.query(SignatureRequest)
                .filter(SignatureRequest.id == request_id)
                .first()
            )
            if orm_req is not None:
                orm_req.status = SignatureStatus.EXECUTED.value
                orm_req.signature_digest = signature_digest
                orm_req.executed_at = exec_time
                if effective_signatory_id is not None:
                    orm_req.signatory_id = effective_signatory_id

                # Transition contract status to ACTIVE if all signature requests for the contract are executed [FR-6]
                contract = (
                    self.db.query(Contract)
                    .filter(Contract.id == orm_req.contract_id)
                    .first()
                )
                if contract is not None:
                    remaining_pending = (
                        self.db.query(SignatureRequest)
                        .filter(
                            SignatureRequest.contract_id == contract.id,
                            SignatureRequest.id != request_id,
                            SignatureRequest.status == SignatureStatus.PENDING.value,
                        )
                        .count()
                    )
                    if remaining_pending == 0:
                        contract.status = ContractStatus.ACTIVE.value
                        contract.updated_at = datetime.now(timezone.utc)

                self.db.commit()

        # Update in-memory cache
        existing.status = SignatureStatus.EXECUTED
        existing.signature_digest = signature_digest
        existing.executed_at = exec_time
        if effective_signatory_id is not None:
            existing.signatory_id = effective_signatory_id
        self._in_memory_requests[request_id] = existing

        # Notify parties if notification_port is configured [FR-6]
        if self.notification_port is not None:
            try:
                recipient_id = str(effective_signatory_id or existing.contract_id)
                if hasattr(self.notification_port, "send_notification"):
                    self.notification_port.send_notification(
                        recipient=recipient_id,
                        subject=f"Signature Executed for Contract {existing.contract_id}",
                        body=(
                            f"Digital signature captured successfully for request '{request_id}'. "
                            f"Digest: {signature_digest}"
                        ),
                        channel="in_app",
                    )
            except Exception as exc:
                logger.warning(
                    "Failed to dispatch signature execution notification",
                    extra={
                        "correlation_id": get_correlation_id(),
                        "request_id": str(request_id),
                        "error": str(exc),
                    },
                )

        logger.info(
            "Captured digital signature execution and updated status",
            extra={
                "correlation_id": get_correlation_id(),
                "request_id": str(request_id),
                "contract_id": str(existing.contract_id),
                "signatory_id": str(effective_signatory_id) if effective_signatory_id else None,
                "status": SignatureStatus.EXECUTED.value,
                "signature_digest": signature_digest,
                "executed_at": exec_time.isoformat(),
            },
        )

        return SignatureExecutionResult(
            request_id=request_id,
            contract_id=existing.contract_id,
            status=SignatureStatus.EXECUTED,
            signature_digest=signature_digest,
            executed_at=exec_time,
            signatory_id=effective_signatory_id,
            success=True,
            message="Signature executed successfully",
        )

    def reject_signature(
        self,
        request_id: UUID,
        reason: str | None = None,
        signatory_id: UUID | None = None,
    ) -> SignatureRequestDomain:
        """Reject an e-signature request and transition status to REJECTED.

        Args:
            request_id: Unique UUID identifier of the signature request.
            reason: Optional textual reason for signature rejection.
            signatory_id: Optional UUID identifier of the rejecting signatory.

        Returns:
            Updated SignatureRequestDomain entity with REJECTED status.

        Raises:
            ValueError: If the signature request is not found or already in a terminal state.
        """
        existing = self.get_signature_request(request_id)
        if existing is None:
            raise ValueError(f"Signature request '{request_id}' not found.")

        current_status = (
            existing.status.value
            if isinstance(existing.status, SignatureStatus)
            else str(existing.status)
        )
        if current_status != SignatureStatus.PENDING.value:
            raise ValueError(
                f"Signature request '{request_id}' is not in PENDING state (current status: '{current_status}')."
            )

        effective_signatory_id = signatory_id or existing.signatory_id

        if self.db is not None:
            orm_req = (
                self.db.query(SignatureRequest)
                .filter(SignatureRequest.id == request_id)
                .first()
            )
            if orm_req is not None:
                orm_req.status = SignatureStatus.REJECTED.value
                if effective_signatory_id is not None:
                    orm_req.signatory_id = effective_signatory_id
                self.db.commit()

        existing.status = SignatureStatus.REJECTED
        if effective_signatory_id is not None:
            existing.signatory_id = effective_signatory_id
        self._in_memory_requests[request_id] = existing

        logger.info(
            "Rejected e-signature request",
            extra={
                "correlation_id": get_correlation_id(),
                "request_id": str(request_id),
                "contract_id": str(existing.contract_id),
                "reason": reason,
                "status": SignatureStatus.REJECTED.value,
            },
        )

        return existing

    def cancel_signature_request(self, request_id: UUID) -> bool:
        """Cancel an active signature request.

        Args:
            request_id: Unique UUID identifier of the signature request to cancel.

        Returns:
            True if the request was successfully cancelled, False if not found.
        """
        found = False

        if self.db is not None:
            orm_req = (
                self.db.query(SignatureRequest)
                .filter(SignatureRequest.id == request_id)
                .first()
            )
            if orm_req is not None:
                self.db.delete(orm_req)
                self.db.commit()
                found = True

        if request_id in self._in_memory_requests:
            del self._in_memory_requests[request_id]
            found = True

        return found


# Alias SignatureConnector to NativeSignatureConnector for module compatibility
SignatureConnector = NativeSignatureConnector
