"""E-signature domain service managing signature workflow state transitions and execution verification [FR-5, FR-6].

Provides signature request initiation, pending signature hub filtering (FR-5), execution
verification (signatory authorization, state check, digest validation), contract state machine
transitions to ACTIVE upon execution completion (FR-6), notification dispatch, and context-correlated
structured audit logging (NFR-1).
"""

from datetime import datetime, timezone
from enum import Enum
import logging
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from src.lib.logging import get_correlation_id
from src.models.contract import Contract, SignatureRequest
from src.services.contract.models import (
    ContractEventType,
    ContractLifecycleEventPayload,
    ContractStatus,
    SignatureRequestDomain,
    SignatureStatus,
)
from src.services.contract.notification_port import NotificationPort
from src.services.contract.signature_port import (
    SignatureExecutionResult,
    SignaturePort,
)

logger = logging.getLogger(__name__)


class SignatureRequestNotFoundError(ValueError):
    """Raised when a requested e-signature request identifier is not found in the repository."""


class InvalidSignatureStateError(ValueError):
    """Raised when an operation is attempted on a signature request or contract in an invalid state."""


class UnauthorizedSignatoryError(PermissionError, ValueError):
    """Raised when a user attempts to execute a signature request assigned to another signatory."""


class SignatureService:
    """Domain service managing signature requests, execution verification, state transitions, and notifications [FR-5, FR-6]."""

    def __init__(
        self,
        db: Session | None = None,
        signature_port: SignaturePort | None = None,
        notification_port: NotificationPort | None = None,
    ) -> None:
        """Initialize SignatureService with optional database session and port adapters.

        Args:
            db: Optional SQLAlchemy Session for synchronous database persistence.
            signature_port: Optional SignaturePort implementation for signature operations.
            notification_port: Optional NotificationPort implementation for sending alerts.
        """
        self.db = db
        self.notification_port = notification_port

        if signature_port is not None:
            self.signature_port = signature_port
        else:
            from src.connectors.signature_connector import NativeSignatureConnector

            self.signature_port = NativeSignatureConnector(
                db=db,
                notification_port=notification_port,
            )

    def _log_signature_event(
        self,
        event_type: ContractEventType | str,
        contract_id: UUID,
        request_id: UUID,
        signatory_id: UUID | None = None,
        correlation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ContractLifecycleEventPayload:
        """Emit context-correlated structured log for signature events [FR-12, NFR-1]."""
        corr_id = correlation_id or get_correlation_id()
        event_meta = {
            "request_id": str(request_id),
            **(metadata or {}),
        }
        payload = ContractLifecycleEventPayload(
            event_type=event_type,
            contract_id=contract_id,
            performed_by_id=signatory_id,
            correlation_id=corr_id,
            metadata=event_meta,
        )
        event_type_str = (
            event_type.value if isinstance(event_type, Enum) else str(event_type)
        )

        logger.info(
            "Contract signature event: %s for request %s (contract %s)",
            event_type_str,
            request_id,
            contract_id,
            extra={
                "correlation_id": corr_id,
                "event_type": event_type_str,
                "contract_id": str(contract_id),
                "request_id": str(request_id),
                "signatory_id": str(signatory_id) if signatory_id else None,
                "metadata": event_meta,
            },
        )
        return payload

    def request_signature(
        self,
        contract_id: UUID,
        signatory_id: UUID | None = None,
        correlation_id: str | None = None,
    ) -> SignatureRequestDomain:
        """Initiate an e-signature request for a contract [FR-5].

        Transitions contract status from DRAFT to PENDING_SIGNATURE if applicable.

        Args:
            contract_id: Unique UUID identifier of target contract.
            signatory_id: Optional UUID identifier of assigned signatory.
            correlation_id: Optional tracking correlation ID for event logging.

        Returns:
            Created SignatureRequestDomain aggregate entity.

        Raises:
            SignatureRequestNotFoundError: If contract_id does not exist in database mode.
            InvalidSignatureStateError: If contract is in TERMINATED or EXPIRED state.
        """
        if self.db is not None:
            contract = (
                self.db.query(Contract).filter(Contract.id == contract_id).first()
            )
            if contract is None:
                raise SignatureRequestNotFoundError(
                    f"Contract '{contract_id}' not found."
                )

            current_status = contract.status
            if current_status in (
                ContractStatus.TERMINATED.value,
                ContractStatus.EXPIRED.value,
            ):
                raise InvalidSignatureStateError(
                    f"Cannot request signature for contract '{contract_id}' in '{current_status}' state."
                )

            if current_status == ContractStatus.DRAFT.value:
                contract.status = ContractStatus.PENDING_SIGNATURE.value
                contract.updated_at = datetime.now(timezone.utc)
                self.db.commit()

        req = self.signature_port.create_signature_request(
            contract_id=contract_id,
            signatory_id=signatory_id,
        )

        self._log_signature_event(
            event_type=ContractEventType.SIGNATURE_REQUESTED,
            contract_id=contract_id,
            request_id=req.id,
            signatory_id=signatory_id,
            correlation_id=correlation_id,
        )

        return req

    def get_signature_request(self, request_id: UUID) -> SignatureRequestDomain | None:
        """Retrieve an e-signature request by its unique UUID identifier."""
        return self.signature_port.get_signature_request(request_id)

    def get_pending_signatures(
        self,
        signatory_id: UUID | None = None,
        contract_id: UUID | None = None,
    ) -> list[SignatureRequestDomain]:
        """Retrieve all e-signature requests currently in PENDING status for the central hub [FR-5]."""
        return self.signature_port.get_pending_signatures(
            signatory_id=signatory_id,
            contract_id=contract_id,
        )

    def list_signature_requests(
        self, contract_id: UUID | None = None
    ) -> list[SignatureRequestDomain]:
        """Retrieve signature requests across all status states, optionally filtered by contract_id."""
        if hasattr(self.signature_port, "list_all_requests"):
            all_reqs = self.signature_port.list_all_requests()
            if contract_id is not None:
                return [r for r in all_reqs if r.contract_id == contract_id]
            return all_reqs

        if self.db is not None:
            query = self.db.query(SignatureRequest)
            if contract_id is not None:
                query = query.filter(SignatureRequest.contract_id == contract_id)
            orm_reqs = query.all()
            return [
                SignatureRequestDomain(
                    id=r.id,
                    contract_id=r.contract_id,
                    signatory_id=r.signatory_id,
                    status=(
                        SignatureStatus(r.status)
                        if r.status in [s.value for s in SignatureStatus]
                        else r.status
                    ),
                    signature_digest=r.signature_digest,
                    executed_at=r.executed_at,
                    created_at=r.created_at,
                )
                for r in orm_reqs
            ]

        return []

    def verify_signature_execution_eligibility(
        self,
        request_id: UUID,
        executing_signatory_id: UUID | None = None,
    ) -> SignatureRequestDomain:
        """Verify that a signature request exists, is in PENDING state, and is authorized for execution [FR-6].

        Args:
            request_id: Unique UUID identifier of signature request.
            executing_signatory_id: Optional UUID identifier of user attempting execution.

        Returns:
            Verified SignatureRequestDomain entity.

        Raises:
            SignatureRequestNotFoundError: If signature request is not found.
            InvalidSignatureStateError: If signature request or contract is not in a signable state.
            UnauthorizedSignatoryError: If executing signatory does not match assigned signatory.
        """
        req = self.get_signature_request(request_id)
        if req is None:
            raise SignatureRequestNotFoundError(
                f"Signature request '{request_id}' not found."
            )

        current_status = (
            req.status.value
            if isinstance(req.status, SignatureStatus)
            else str(req.status)
        )
        if current_status != SignatureStatus.PENDING.value:
            raise InvalidSignatureStateError(
                f"Signature request '{request_id}' is not in PENDING state (current status: '{current_status}')."
            )

        if self.db is not None:
            contract = (
                self.db.query(Contract).filter(Contract.id == req.contract_id).first()
            )
            if contract is not None and contract.status in (
                ContractStatus.TERMINATED.value,
                ContractStatus.EXPIRED.value,
            ):
                raise InvalidSignatureStateError(
                    f"Contract '{req.contract_id}' is in '{contract.status}' state and cannot accept signatures."
                )

        if (
            req.signatory_id is not None
            and executing_signatory_id is not None
            and req.signatory_id != executing_signatory_id
        ):
            raise UnauthorizedSignatoryError(
                f"User '{executing_signatory_id}' is not authorized to sign request assigned to '{req.signatory_id}'."
            )

        return req

    def execute_signature(
        self,
        request_id: UUID,
        signature_digest: str,
        signatory_id: UUID | None = None,
        executed_at: datetime | None = None,
        correlation_id: str | None = None,
    ) -> SignatureExecutionResult:
        """Capture digital signature execution, transition contract status to ACTIVE, and notify parties [FR-6].

        Args:
            request_id: Unique UUID identifier of signature request.
            signature_digest: Non-empty cryptographic signature digest or payload hash.
            signatory_id: Optional UUID identifier of executing signatory.
            executed_at: Optional explicit execution timestamp.
            correlation_id: Optional tracking correlation ID for logging.

        Returns:
            SignatureExecutionResult aggregate status DTO.

        Raises:
            ValueError: If signature digest is empty or invalid.
            SignatureRequestNotFoundError: If request is not found.
            InvalidSignatureStateError: If request is not in PENDING state.
            UnauthorizedSignatoryError: If signatory is unauthorized.
        """
        if not signature_digest or not signature_digest.strip():
            raise ValueError("Signature digest cannot be empty.")

        req = self.verify_signature_execution_eligibility(
            request_id=request_id,
            executing_signatory_id=signatory_id,
        )

        exec_result = self.signature_port.execute_signature(
            request_id=request_id,
            signature_digest=signature_digest,
            signatory_id=signatory_id,
            executed_at=executed_at,
        )

        # Transition contract status to ACTIVE if database session is active
        if self.db is not None:
            contract = (
                self.db.query(Contract).filter(Contract.id == req.contract_id).first()
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

        # Notify parties if notification_port is available [FR-6]
        if self.notification_port is not None:
            try:
                recipient_id = str(
                    signatory_id or req.signatory_id or req.contract_id
                )
                if hasattr(self.notification_port, "send_notification"):
                    self.notification_port.send_notification(
                        recipient=recipient_id,
                        subject=f"Signature Executed for Contract {req.contract_id}",
                        body=(
                            f"Digital signature captured successfully for request '{request_id}'. "
                            f"Contract state updated to EXECUTED/ACTIVE."
                        ),
                        channel="in_app",
                    )
            except Exception as exc:
                logger.warning(
                    "Failed to dispatch signature execution notification via notification_port",
                    extra={
                        "correlation_id": correlation_id or get_correlation_id(),
                        "request_id": str(request_id),
                        "error": str(exc),
                    },
                )

        self._log_signature_event(
            event_type=ContractEventType.SIGNATURE_EXECUTED,
            contract_id=req.contract_id,
            request_id=request_id,
            signatory_id=signatory_id or req.signatory_id,
            correlation_id=correlation_id,
            metadata={
                "signature_digest": signature_digest,
                "executed_at": exec_result.executed_at.isoformat(),
            },
        )

        return exec_result

    def reject_signature(
        self,
        request_id: UUID,
        reason: str | None = None,
        signatory_id: UUID | None = None,
        correlation_id: str | None = None,
    ) -> SignatureRequestDomain:
        """Reject an e-signature request and transition request status to REJECTED.

        Args:
            request_id: Unique UUID identifier of signature request.
            reason: Optional textual reason for rejection.
            signatory_id: Optional UUID identifier of rejecting signatory.
            correlation_id: Optional correlation tracking ID.

        Returns:
            Updated SignatureRequestDomain with REJECTED status.
        """
        self.verify_signature_execution_eligibility(
            request_id=request_id,
            executing_signatory_id=signatory_id,
        )

        updated_req = self.signature_port.reject_signature(
            request_id=request_id,
            reason=reason,
            signatory_id=signatory_id,
        )

        self._log_signature_event(
            event_type=ContractEventType.SIGNATURE_REJECTED,
            contract_id=updated_req.contract_id,
            request_id=request_id,
            signatory_id=signatory_id,
            correlation_id=correlation_id,
            metadata={"reason": reason or ""},
        )

        return updated_req

    def cancel_signature_request(
        self,
        request_id: UUID,
        correlation_id: str | None = None,
    ) -> bool:
        """Cancel an active signature request."""
        req = self.get_signature_request(request_id)
        success = self.signature_port.cancel_signature_request(request_id)

        if success and req is not None:
            logger.info(
                "Cancelled signature request %s for contract %s",
                request_id,
                req.contract_id,
                extra={
                    "correlation_id": correlation_id or get_correlation_id(),
                    "request_id": str(request_id),
                    "contract_id": str(req.contract_id),
                },
            )

        return success
