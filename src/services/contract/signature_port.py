"""Abstract e-signature tracking interface definition for contract lifecycle management.

This module defines the abstract port contract and execution DTOs for digital signature
requests, status tracking, and execution capture [FR-5, FR-6]. Decouples core business
logic from external e-signature connectors (e.g., native signing, third-party providers).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from src.services.contract.models import SignatureRequestDomain, SignatureStatus


@dataclass
class SignatureExecutionResult:
    """Standardized DTO representing the result of a digital signature execution capture [FR-6]."""

    request_id: UUID
    contract_id: UUID
    status: SignatureStatus | str
    signature_digest: str | None
    executed_at: datetime
    signatory_id: UUID | None = None
    success: bool = True
    message: str = "Signature executed successfully"


class SignaturePort(ABC):
    """Abstract port contract for digital e-signature tracking and execution operations."""

    @abstractmethod
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
        ...

    @abstractmethod
    def get_signature_request(self, request_id: UUID) -> SignatureRequestDomain | None:
        """Retrieve an e-signature request by its unique UUID identifier.

        Args:
            request_id: Unique UUID identifier of the signature request.

        Returns:
            SignatureRequestDomain entity if found, None otherwise.
        """
        ...

    @abstractmethod
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
        ...

    @abstractmethod
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
        ...

    @abstractmethod
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
        ...

    @abstractmethod
    def cancel_signature_request(self, request_id: UUID) -> bool:
        """Cancel an active signature request.

        Args:
            request_id: Unique UUID identifier of the signature request to cancel.

        Returns:
            True if the request was successfully cancelled, False if not found.
        """
        ...
