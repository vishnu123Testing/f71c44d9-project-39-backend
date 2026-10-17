"""Domain entities, value objects, dataclasses, lifecycle state enums, and event payloads for contract management."""

from dataclasses import dataclass, field
from datetime import date as date_type, datetime, timezone
from enum import Enum
from typing import Any
import uuid
from uuid import UUID


class ContractCategory(str, Enum):
    """Supported contract category classifications."""

    CONTRACT = "Contract"
    SOW = "SOW"
    NDA = "NDA"
    EXHIBIT = "Exhibit"


class ContractStatus(str, Enum):
    """Contract lifecycle status states."""

    DRAFT = "DRAFT"
    PENDING_SIGNATURE = "PENDING_SIGNATURE"
    ACTIVE = "ACTIVE"
    AMENDED = "AMENDED"
    TERMINATED = "TERMINATED"
    EXPIRED = "EXPIRED"


class SignatureStatus(str, Enum):
    """Status states for e-signature requests."""

    PENDING = "PENDING"
    EXECUTED = "EXECUTED"
    REJECTED = "REJECTED"


class ContractEventType(str, Enum):
    """Contract lifecycle event classifications for audit logging and events."""

    CREATION = "CREATION"
    DOCUMENT_UPLOADED = "DOCUMENT_UPLOADED"
    SIGNATURE_REQUESTED = "SIGNATURE_REQUESTED"
    SIGNATURE_EXECUTED = "SIGNATURE_EXECUTED"
    SIGNATURE_REJECTED = "SIGNATURE_REJECTED"
    AMENDMENT_ADDED = "AMENDMENT_ADDED"
    STATUS_CHANGED = "STATUS_CHANGED"
    RENEWAL_ALERT_DISPATCHED = "RENEWAL_ALERT_DISPATCHED"
    TERMINATION = "TERMINATION"


@dataclass
class ContractClauseDomain:
    """Domain entity representing a contract clause."""

    id: UUID
    contract_id: UUID
    clause_type: str
    title: str
    body_text: str
    is_highlighted: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ContractSLADomain:
    """Domain entity representing a contract SLA metric and target."""

    id: UUID
    contract_id: UUID
    metric_name: str
    target_value: str
    penalty_terms: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class DocumentVersionDomain:
    """Domain entity representing a sequential version-controlled document payload."""

    id: UUID
    contract_id: UUID
    category_type: str
    version_number: int
    file_path: str
    checksum: str
    uploaded_by_id: UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ContractAmendmentDomain:
    """Domain entity representing an immutable append-only amendment record."""

    id: UUID
    contract_id: UUID
    sequence_number: int
    summary: str
    author_id: UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class SignatureRequestDomain:
    """Domain entity representing a digital signature request and execution state."""

    id: UUID
    contract_id: UUID
    signatory_id: UUID | None = None
    status: SignatureStatus | str = SignatureStatus.PENDING
    signature_digest: str | None = None
    executed_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class RenewalAlertDomain:
    """Domain entity representing a contract renewal alert dispatch."""

    id: UUID
    contract_id: UUID
    threshold_days: int
    dispatched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ContractDomain:
    """Core domain aggregate entity for a contract."""

    id: UUID
    title: str
    category: ContractCategory | str
    counterparty: str
    status: ContractStatus | str = ContractStatus.DRAFT
    start_on: date_type | None = None
    expires_on: date_type | None = None
    parent_contract_id: UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    clauses: list[ContractClauseDomain] = field(default_factory=list)
    slas: list[ContractSLADomain] = field(default_factory=list)
    document_versions: list[DocumentVersionDomain] = field(default_factory=list)
    amendments: list[ContractAmendmentDomain] = field(default_factory=list)
    signature_requests: list[SignatureRequestDomain] = field(default_factory=list)
    renewal_alerts: list[RenewalAlertDomain] = field(default_factory=list)


@dataclass
class ContractLifecycleEventPayload:
    """Structured event payload capturing contract lifecycle events and audit metadata [FR-12]."""

    event_type: ContractEventType | str
    contract_id: UUID
    event_id: UUID = field(default_factory=uuid.uuid4)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    performed_by_id: UUID | None = None
    correlation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize event payload to a JSON-compatible dictionary for structured audit logging."""
        event_type_val = (
            self.event_type.value
            if isinstance(self.event_type, Enum)
            else str(self.event_type)
        )

        def _serialize_val(val: Any) -> Any:
            if isinstance(val, UUID):
                return str(val)
            if isinstance(val, datetime):
                return val.isoformat()
            if isinstance(val, date_type):
                return val.isoformat()
            if isinstance(val, Enum):
                return val.value
            if isinstance(val, dict):
                return {k: _serialize_val(v) for k, v in val.items()}
            if isinstance(val, list):
                return [_serialize_val(item) for item in val]
            return val

        return {
            "event_id": str(self.event_id),
            "event_type": event_type_val,
            "contract_id": str(self.contract_id),
            "timestamp": self.timestamp.isoformat(),
            "performed_by_id": str(self.performed_by_id) if self.performed_by_id else None,
            "correlation_id": self.correlation_id,
            "metadata": _serialize_val(self.metadata),
        }


@dataclass
class ContractCreationEventPayload(ContractLifecycleEventPayload):
    """Payload specifically for contract creation lifecycle events."""

    def __init__(
        self,
        contract_id: UUID,
        title: str,
        category: str,
        counterparty: str,
        performed_by_id: UUID | None = None,
        correlation_id: str | None = None,
        start_on: date_type | None = None,
        expires_on: date_type | None = None,
        **extra_metadata: Any,
    ) -> None:
        metadata = {
            "title": title,
            "category": category,
            "counterparty": counterparty,
            "start_on": start_on.isoformat() if start_on else None,
            "expires_on": expires_on.isoformat() if expires_on else None,
            **extra_metadata,
        }
        super().__init__(
            event_type=ContractEventType.CREATION,
            contract_id=contract_id,
            performed_by_id=performed_by_id,
            correlation_id=correlation_id,
            metadata=metadata,
        )


@dataclass
class ContractSignedEventPayload(ContractLifecycleEventPayload):
    """Payload specifically for contract e-signature execution events."""

    def __init__(
        self,
        contract_id: UUID,
        signature_request_id: UUID,
        signatory_id: UUID | None,
        signature_digest: str | None,
        executed_at: datetime | None = None,
        correlation_id: str | None = None,
        **extra_metadata: Any,
    ) -> None:
        exec_time = executed_at or datetime.now(timezone.utc)
        metadata = {
            "signature_request_id": str(signature_request_id),
            "signatory_id": str(signatory_id) if signatory_id else None,
            "signature_digest": signature_digest,
            "executed_at": exec_time.isoformat(),
            **extra_metadata,
        }
        super().__init__(
            event_type=ContractEventType.SIGNATURE_EXECUTED,
            contract_id=contract_id,
            performed_by_id=signatory_id,
            correlation_id=correlation_id,
            metadata=metadata,
        )


@dataclass
class ContractAmendedEventPayload(ContractLifecycleEventPayload):
    """Payload specifically for contract amendment lifecycle events."""

    def __init__(
        self,
        contract_id: UUID,
        amendment_id: UUID,
        sequence_number: int,
        summary: str,
        author_id: UUID | None = None,
        correlation_id: str | None = None,
        **extra_metadata: Any,
    ) -> None:
        metadata = {
            "amendment_id": str(amendment_id),
            "sequence_number": sequence_number,
            "summary": summary,
            "author_id": str(author_id) if author_id else None,
            **extra_metadata,
        }
        super().__init__(
            event_type=ContractEventType.AMENDMENT_ADDED,
            contract_id=contract_id,
            performed_by_id=author_id,
            correlation_id=correlation_id,
            metadata=metadata,
        )


@dataclass
class ContractRenewalAlertEventPayload(ContractLifecycleEventPayload):
    """Payload specifically for contract renewal alert notification events."""

    def __init__(
        self,
        contract_id: UUID,
        alert_id: UUID,
        threshold_days: int,
        expires_on: date_type | None = None,
        correlation_id: str | None = None,
        **extra_metadata: Any,
    ) -> None:
        metadata = {
            "alert_id": str(alert_id),
            "threshold_days": threshold_days,
            "expires_on": expires_on.isoformat() if expires_on else None,
            **extra_metadata,
        }
        super().__init__(
            event_type=ContractEventType.RENEWAL_ALERT_DISPATCHED,
            contract_id=contract_id,
            performed_by_id=None,
            correlation_id=correlation_id,
            metadata=metadata,
        )
