"""Amendment domain service enforcing immutable append-only audit trail operations for contracts [FR-7, FR-8, NFR-4].

Provides amendment creation with sequential versioning, read-only audit timeline retrieval,
and explicit safeguards blocking modification, deletion, or truncation of historical records.
"""

from datetime import datetime, timezone
from enum import Enum
import logging
from typing import Any
import uuid
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.event import listens_for
from sqlalchemy.orm import Session

from src.lib.logging import get_correlation_id
from src.models.contract import Contract, ContractAmendment
from src.services.contract.models import (
    ContractAmendmentDomain,
    ContractEventType,
    ContractLifecycleEventPayload,
    ContractStatus,
)

logger = logging.getLogger(__name__)


class ImmutableAuditTrailError(PermissionError):
    """Raised when an operation attempts to modify, delete, or truncate immutable amendment audit trail entries [FR-8, NFR-4]."""


@listens_for(ContractAmendment, "before_update")
def _block_amendment_update_listener(mapper: Any, connection: Any, target: ContractAmendment) -> None:
    """SQLAlchemy ORM event listener blocking UPDATE operations on ContractAmendment instances [FR-8, NFR-4]."""
    raise ImmutableAuditTrailError(
        f"Contract amendment record {target.id} is immutable. "
        "Editing or updating existing amendment audit entries is strictly forbidden [FR-8, NFR-4]."
    )


@listens_for(ContractAmendment, "before_delete")
def _block_amendment_delete_listener(mapper: Any, connection: Any, target: ContractAmendment) -> None:
    """SQLAlchemy ORM event listener blocking DELETE operations on ContractAmendment instances [FR-8, NFR-4]."""
    raise ImmutableAuditTrailError(
        f"Contract amendment record {target.id} is immutable. "
        "Deleting amendment audit entries is strictly forbidden [FR-8, NFR-4]."
    )


def to_amendment_domain_entity(amendment: ContractAmendment) -> ContractAmendmentDomain:
    """Map a SQLAlchemy ContractAmendment ORM instance to a pure ContractAmendmentDomain entity."""
    return ContractAmendmentDomain(
        id=amendment.id,
        contract_id=amendment.contract_id,
        sequence_number=amendment.sequence_number,
        summary=amendment.summary,
        author_id=amendment.author_id,
        created_at=amendment.created_at,
    )


class AmendmentService:
    """Domain service managing contract amendment operations enforcing immutable append-only audit trail integrity [FR-7, FR-8, NFR-4]."""

    def __init__(self, db: Session) -> None:
        """Initialize AmendmentService with a synchronous SQLAlchemy database session."""
        self.db = db

    def _log_amendment_event(
        self,
        contract_id: UUID,
        amendment_id: UUID,
        sequence_number: int,
        summary: str,
        author_id: UUID | None = None,
        correlation_id: str | None = None,
    ) -> ContractLifecycleEventPayload:
        """Emit context-correlated structured log for amendment addition [FR-12, NFR-1]."""
        corr_id = correlation_id or get_correlation_id()
        payload = ContractLifecycleEventPayload(
            event_type=ContractEventType.AMENDMENT_ADDED,
            contract_id=contract_id,
            performed_by_id=author_id,
            correlation_id=corr_id,
            metadata={
                "amendment_id": str(amendment_id),
                "sequence_number": sequence_number,
                "summary": summary,
                "author_id": str(author_id) if author_id else None,
            },
        )

        logger.info(
            "Contract amendment appended: sequence %d for contract %s",
            sequence_number,
            contract_id,
            extra={
                "correlation_id": corr_id,
                "event_id": str(payload.event_id),
                "event_type": ContractEventType.AMENDMENT_ADDED.value,
                "contract_id": str(contract_id),
                "performed_by_id": str(author_id) if author_id else None,
                "metadata": payload.to_dict()["metadata"],
            },
        )
        return payload

    def add_amendment(
        self,
        contract_id: UUID,
        summary: str,
        author_id: UUID | None = None,
        performed_by_id: UUID | None = None,
        correlation_id: str | None = None,
    ) -> ContractAmendmentDomain:
        """Append a new amendment to a contract's immutable audit trail [FR-7].

        Args:
            contract_id: UUID of the target contract.
            summary: Text summary describing the contract terms amendment.
            author_id: UUID of the author making the amendment.
            performed_by_id: Fallback UUID of the user performing the action.
            correlation_id: Optional correlation tracking identifier.

        Returns:
            ContractAmendmentDomain pure domain entity representing the appended audit entry.

        Raises:
            ValueError: If summary is empty or target contract does not exist.
        """
        if not summary or not summary.strip():
            raise ValueError("Amendment summary cannot be empty.")

        effective_author_id = author_id or performed_by_id

        contract = self.db.query(Contract).filter(Contract.id == contract_id).first()
        if not contract:
            raise ValueError(f"Contract with ID {contract_id} not found.")

        # Compute next sequence number for this contract's amendment history
        max_seq = (
            self.db.query(func.max(ContractAmendment.sequence_number))
            .filter(ContractAmendment.contract_id == contract_id)
            .scalar()
        )
        next_sequence = (max_seq or 0) + 1

        amendment = ContractAmendment(
            id=uuid.uuid4(),
            contract_id=contract_id,
            author_id=effective_author_id,
            sequence_number=next_sequence,
            summary=summary.strip(),
            created_at=datetime.now(timezone.utc),
        )

        # Update contract status to AMENDED if active or draft
        if contract.status in [ContractStatus.ACTIVE.value, ContractStatus.DRAFT.value, ContractStatus.PENDING_SIGNATURE.value]:
            contract.status = ContractStatus.AMENDED.value
        contract.updated_at = datetime.now(timezone.utc)

        self.db.add(amendment)
        self.db.flush()
        self.db.commit()

        self._log_amendment_event(
            contract_id=contract_id,
            amendment_id=amendment.id,
            sequence_number=next_sequence,
            summary=amendment.summary,
            author_id=effective_author_id,
            correlation_id=correlation_id,
        )

        return to_amendment_domain_entity(amendment)

    def get_amendments_for_contract(self, contract_id: UUID) -> list[ContractAmendmentDomain]:
        """Retrieve chronological read-only amendment audit entries for a contract [FR-7, FR-8].

        Args:
            contract_id: UUID of the contract.

        Returns:
            List of ContractAmendmentDomain entities ordered by sequence number ascending.
        """
        records = (
            self.db.query(ContractAmendment)
            .filter(ContractAmendment.contract_id == contract_id)
            .order_by(ContractAmendment.sequence_number.asc())
            .all()
        )
        return [to_amendment_domain_entity(rec) for rec in records]

    def get_amendment_by_id(self, amendment_id: UUID) -> ContractAmendmentDomain | None:
        """Fetch a single amendment audit log entry by its UUID.

        Args:
            amendment_id: UUID of the amendment record.

        Returns:
            ContractAmendmentDomain entity or None if not found.
        """
        record = self.db.query(ContractAmendment).filter(ContractAmendment.id == amendment_id).first()
        if not record:
            return None
        return to_amendment_domain_entity(record)

    def get_amendment_count(self, contract_id: UUID) -> int:
        """Return the total number of amendment audit records for a contract."""
        return (
            self.db.query(func.count(ContractAmendment.id))
            .filter(ContractAmendment.contract_id == contract_id)
            .scalar()
            or 0
        )

    def update_amendment(self, amendment_id: UUID, **kwargs: Any) -> None:
        """Explicitly block and reject any attempt to modify an existing amendment audit entry [FR-8, NFR-4].

        Raises:
            ImmutableAuditTrailError: Always raised to preserve audit trail non-mutability.
        """
        raise ImmutableAuditTrailError(
            f"Operation rejected: Contract amendment record {amendment_id} is immutable and cannot be updated [FR-8, NFR-4]."
        )

    def delete_amendment(self, amendment_id: UUID) -> None:
        """Explicitly block and reject any attempt to delete an existing amendment audit entry [FR-8, NFR-4].

        Raises:
            ImmutableAuditTrailError: Always raised to preserve audit trail non-mutability.
        """
        raise ImmutableAuditTrailError(
            f"Operation rejected: Contract amendment record {amendment_id} is immutable and cannot be deleted [FR-8, NFR-4]."
        )

    def clear_amendments(self, contract_id: UUID) -> None:
        """Explicitly block and reject any attempt to bulk delete or truncate amendment records [FR-8, NFR-4].

        Raises:
            ImmutableAuditTrailError: Always raised to preserve audit trail non-mutability.
        """
        raise ImmutableAuditTrailError(
            f"Operation rejected: Bulk deletion or truncation of amendment audit records for contract {contract_id} is strictly prohibited [FR-8, NFR-4]."
        )
