"""Core contract lifecycle service implementing business logic, versioning, SLA evaluation, search filtering, and structured logging.

Handles agreement creation, sequential version control (FR-4), SLA and clause tracking (FR-1),
multi-criteria repository filtering (FR-11), structured audit event metadata generation (FR-12),
and context-correlated structured logging (NFR-1).
"""

from datetime import date as date_type, datetime, timezone
from enum import Enum
import hashlib
import logging
from typing import Any
import uuid
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from src.lib.logging import get_correlation_id
from src.models.contract import Contract, ContractClause, ContractSLA, DocumentVersion
from src.services.contract.models import (
    ContractAmendmentDomain,
    ContractCategory,
    ContractClauseDomain,
    ContractCreationEventPayload,
    ContractDomain,
    ContractEventType,
    ContractLifecycleEventPayload,
    ContractSLADomain,
    ContractStatus,
    DocumentVersionDomain,
    RenewalAlertDomain,
    SignatureRequestDomain,
    SignatureStatus,
)
from src.services.contract.notification_port import NotificationPort
from src.services.contract.storage_port import StoragePort

logger = logging.getLogger(__name__)


def to_domain_entity(contract: Contract) -> ContractDomain:
    """Map a SQLAlchemy Contract ORM instance to a pure domain entity ContractDomain."""
    category_val = (
        ContractCategory(contract.category)
        if contract.category in [c.value for c in ContractCategory]
        else contract.category
    )
    status_val = (
        ContractStatus(contract.status)
        if contract.status in [s.value for s in ContractStatus]
        else contract.status
    )

    return ContractDomain(
        id=contract.id,
        title=contract.title,
        category=category_val,
        counterparty=contract.counterparty,
        status=status_val,
        start_on=contract.start_on,
        expires_on=contract.expires_on,
        parent_contract_id=contract.parent_contract_id,
        created_at=contract.created_at,
        updated_at=contract.updated_at,
        clauses=[
            ContractClauseDomain(
                id=c.id,
                contract_id=c.contract_id,
                clause_type=c.clause_type,
                title=c.title,
                body_text=c.body_text,
                is_highlighted=c.is_highlighted,
                created_at=c.created_at,
            )
            for c in (contract.clauses or [])
        ],
        slas=[
            ContractSLADomain(
                id=s.id,
                contract_id=s.contract_id,
                metric_name=s.metric_name,
                target_value=s.target_value,
                penalty_terms=s.penalty_terms,
                created_at=s.created_at,
            )
            for s in (contract.slas or [])
        ],
        document_versions=[
            DocumentVersionDomain(
                id=d.id,
                contract_id=d.contract_id,
                category_type=d.category_type,
                version_number=d.version_number,
                file_path=d.file_path,
                checksum=d.checksum,
                uploaded_by_id=d.uploaded_by_id,
                created_at=d.created_at,
            )
            for d in (contract.document_versions or [])
        ],
        amendments=[
            ContractAmendmentDomain(
                id=a.id,
                contract_id=a.contract_id,
                sequence_number=a.sequence_number,
                summary=a.summary,
                author_id=a.author_id,
                created_at=a.created_at,
            )
            for a in (contract.amendments or [])
        ],
        signature_requests=[
            SignatureRequestDomain(
                id=sr.id,
                contract_id=sr.contract_id,
                signatory_id=sr.signatory_id,
                status=(
                    SignatureStatus(sr.status)
                    if sr.status in [st.value for st in SignatureStatus]
                    else sr.status
                ),
                signature_digest=sr.signature_digest,
                executed_at=sr.executed_at,
                created_at=sr.created_at,
            )
            for sr in (contract.signature_requests or [])
        ],
        renewal_alerts=[
            RenewalAlertDomain(
                id=ra.id,
                contract_id=ra.contract_id,
                threshold_days=ra.threshold_days,
                dispatched_at=ra.dispatched_at,
            )
            for ra in (contract.renewal_alerts or [])
        ],
    )


class ContractLifecycleService:
    """Core domain lifecycle service executing contract rules, version control, and event logging."""

    def __init__(
        self,
        db: Session,
        storage_port: StoragePort | None = None,
        notification_port: NotificationPort | None = None,
    ) -> None:
        """Initialize ContractLifecycleService with synchronous database session and ports."""
        self.db = db
        self.storage_port = storage_port
        self.notification_port = notification_port

    def _log_lifecycle_event(
        self,
        event_type: ContractEventType | str,
        contract_id: UUID,
        performed_by_id: UUID | None = None,
        correlation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ContractLifecycleEventPayload:
        """Emit context-correlated structured log and capture event metadata [FR-12, NFR-1]."""
        corr_id = correlation_id or get_correlation_id()
        payload = ContractLifecycleEventPayload(
            event_type=event_type,
            contract_id=contract_id,
            performed_by_id=performed_by_id,
            correlation_id=corr_id,
            metadata=metadata or {},
        )
        event_type_str = (
            event_type.value if isinstance(event_type, Enum) else str(event_type)
        )

        logger.info(
            "Contract lifecycle event: %s for contract %s",
            event_type_str,
            contract_id,
            extra={
                "correlation_id": corr_id,
                "event_id": str(payload.event_id),
                "event_type": event_type_str,
                "contract_id": str(contract_id),
                "performed_by_id": str(performed_by_id) if performed_by_id else None,
                "metadata": payload.to_dict()["metadata"],
            },
        )
        return payload

    def create_contract(
        self,
        title: str,
        category: ContractCategory | str,
        counterparty: str,
        start_on: date_type | None = None,
        expires_on: date_type | None = None,
        parent_contract_id: UUID | None = None,
        performed_by_id: UUID | None = None,
        correlation_id: str | None = None,
        clauses: list[dict[str, Any]] | None = None,
        slas: list[dict[str, Any]] | None = None,
    ) -> Contract:
        """Create a new contract agreement record and emit CREATION event payload [FR-3, FR-12]."""
        category_str = (
            category.value if isinstance(category, Enum) else str(category)
        )

        if parent_contract_id is not None:
            parent = (
                self.db.query(Contract)
                .filter(Contract.id == parent_contract_id)
                .first()
            )
            if not parent:
                raise ValueError(f"Parent contract {parent_contract_id} not found")

        contract = Contract(
            title=title.strip(),
            category=category_str,
            status=ContractStatus.DRAFT.value,
            counterparty=counterparty.strip(),
            start_on=start_on,
            expires_on=expires_on,
            parent_contract_id=parent_contract_id,
        )
        self.db.add(contract)
        self.db.flush()

        if clauses:
            for clause_data in clauses:
                clause = ContractClause(
                    contract_id=contract.id,
                    clause_type=clause_data["clause_type"],
                    title=clause_data["title"],
                    body_text=clause_data["body_text"],
                    is_highlighted=clause_data.get("is_highlighted", False),
                )
                self.db.add(clause)

        if slas:
            for sla_data in slas:
                sla = ContractSLA(
                    contract_id=contract.id,
                    metric_name=sla_data["metric_name"],
                    target_value=sla_data["target_value"],
                    penalty_terms=sla_data.get("penalty_terms"),
                )
                self.db.add(sla)

        self.db.commit()
        self.db.refresh(contract)

        event_payload = ContractCreationEventPayload(
            contract_id=contract.id,
            title=contract.title,
            category=contract.category,
            counterparty=contract.counterparty,
            performed_by_id=performed_by_id,
            correlation_id=correlation_id or get_correlation_id(),
            start_on=contract.start_on,
            expires_on=contract.expires_on,
        )
        self._log_lifecycle_event(
            event_type=ContractEventType.CREATION,
            contract_id=contract.id,
            performed_by_id=performed_by_id,
            correlation_id=correlation_id,
            metadata=event_payload.to_dict()["metadata"],
        )

        return contract

    def get_contract_by_id(self, contract_id: UUID) -> Contract | None:
        """Fetch contract record by primary key UUID [FR-1]."""
        return self.db.query(Contract).filter(Contract.id == contract_id).first()

    def update_contract(
        self,
        contract_id: UUID,
        title: str | None = None,
        category: ContractCategory | str | None = None,
        counterparty: str | None = None,
        status: ContractStatus | str | None = None,
        start_on: date_type | None = None,
        expires_on: date_type | None = None,
        performed_by_id: UUID | None = None,
        correlation_id: str | None = None,
    ) -> Contract:
        """Update contract record details and emit status change event if status transitions."""
        contract = self.get_contract_by_id(contract_id)
        if not contract:
            raise ValueError(f"Contract {contract_id} not found")

        old_status = contract.status

        if title is not None:
            contract.title = title.strip()
        if category is not None:
            contract.category = (
                category.value if isinstance(category, Enum) else str(category)
            )
        if counterparty is not None:
            contract.counterparty = counterparty.strip()
        if start_on is not None:
            contract.start_on = start_on
        if expires_on is not None:
            contract.expires_on = expires_on

        if status is not None:
            new_status_str = (
                status.value if isinstance(status, Enum) else str(status)
            )
            if new_status_str != old_status:
                contract.status = new_status_str
                self._log_lifecycle_event(
                    event_type=ContractEventType.STATUS_CHANGED,
                    contract_id=contract.id,
                    performed_by_id=performed_by_id,
                    correlation_id=correlation_id,
                    metadata={"old_status": old_status, "new_status": new_status_str},
                )

        contract.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(contract)
        return contract

    def delete_contract(
        self,
        contract_id: UUID,
        performed_by_id: UUID | None = None,
        correlation_id: str | None = None,
    ) -> bool:
        """Delete contract record and associated physical documents via StoragePort."""
        contract = self.get_contract_by_id(contract_id)
        if not contract:
            return False

        if self.storage_port:
            for version in contract.document_versions:
                try:
                    self.storage_port.delete_document(version.file_path)
                except Exception as exc:
                    logger.warning(
                        "Failed to delete stored document payload at %s: %s",
                        version.file_path,
                        exc,
                    )

        self._log_lifecycle_event(
            event_type=ContractEventType.TERMINATION,
            contract_id=contract.id,
            performed_by_id=performed_by_id,
            correlation_id=correlation_id,
            metadata={"action": "contract_deleted", "title": contract.title},
        )

        self.db.delete(contract)
        self.db.commit()
        return True

    def search_contracts(
        self,
        category: ContractCategory | str | None = None,
        status: ContractStatus | str | None = None,
        counterparty: str | None = None,
        expires_before: date_type | None = None,
        expires_after: date_type | None = None,
        search_query: str | None = None,
        parent_contract_id: UUID | None = None,
        page: int = 1,
        limit: int = 20,
    ) -> tuple[list[Contract], int]:
        """Multi-criteria repository filtering across category, status, expiration date, counterparty [FR-11]."""
        query = self.db.query(Contract)

        if category is not None:
            cat_str = category.value if isinstance(category, Enum) else str(category)
            query = query.filter(Contract.category == cat_str)

        if status is not None:
            stat_str = status.value if isinstance(status, Enum) else str(status)
            query = query.filter(Contract.status == stat_str)

        if counterparty:
            query = query.filter(Contract.counterparty.ilike(f"%{counterparty.strip()}%"))

        if expires_before is not None:
            query = query.filter(Contract.expires_on <= expires_before)

        if expires_after is not None:
            query = query.filter(Contract.expires_on >= expires_after)

        if search_query:
            pattern = f"%{search_query.strip()}%"
            query = query.filter(
                or_(
                    Contract.title.ilike(pattern),
                    Contract.counterparty.ilike(pattern),
                )
            )

        if parent_contract_id is not None:
            query = query.filter(Contract.parent_contract_id == parent_contract_id)

        total_count = query.count()

        safe_page = max(1, page)
        safe_limit = max(1, min(100, limit))
        offset = (safe_page - 1) * safe_limit

        results = (
            query.order_by(Contract.created_at.desc())
            .offset(offset)
            .limit(safe_limit)
            .all()
        )

        return results, total_count

    def upload_document_version(
        self,
        contract_id: UUID,
        category_type: ContractCategory | str,
        file_name: str,
        content: bytes,
        uploaded_by_id: UUID | None = None,
        correlation_id: str | None = None,
    ) -> DocumentVersion:
        """Upload a document file payload, increment version number sequentially, and store payload [FR-4]."""
        contract = self.get_contract_by_id(contract_id)
        if not contract:
            raise ValueError(f"Contract {contract_id} not found")

        cat_str = (
            category_type.value
            if isinstance(category_type, Enum)
            else str(category_type)
        )

        max_version = (
            self.db.query(func.max(DocumentVersion.version_number))
            .filter(DocumentVersion.contract_id == contract_id)
            .scalar()
        )
        next_version = (max_version or 0) + 1

        file_path = f"/storage/contracts/{contract_id}/v{next_version}_{file_name}"
        checksum = hashlib.sha256(content).hexdigest()

        if self.storage_port:
            stored_res = self.storage_port.store_document(
                contract_id=contract_id,
                category_type=cat_str,
                version_number=next_version,
                file_name=file_name,
                content=content,
            )
            file_path = stored_res.file_path
            checksum = stored_res.checksum

        doc_version = DocumentVersion(
            contract_id=contract_id,
            uploaded_by_id=uploaded_by_id,
            category_type=cat_str,
            version_number=next_version,
            file_path=file_path,
            checksum=checksum,
        )
        self.db.add(doc_version)
        self.db.commit()
        self.db.refresh(doc_version)

        self._log_lifecycle_event(
            event_type=ContractEventType.DOCUMENT_UPLOADED,
            contract_id=contract_id,
            performed_by_id=uploaded_by_id,
            correlation_id=correlation_id,
            metadata={
                "version_id": str(doc_version.id),
                "category_type": cat_str,
                "version_number": next_version,
                "file_path": file_path,
                "checksum": checksum,
                "file_name": file_name,
                "file_size": len(content),
            },
        )

        return doc_version

    def get_document_versions(self, contract_id: UUID) -> list[DocumentVersion]:
        """Retrieve all document versions for a contract ordered by version number [FR-4]."""
        return (
            self.db.query(DocumentVersion)
            .filter(DocumentVersion.contract_id == contract_id)
            .order_by(DocumentVersion.version_number.asc())
            .all()
        )

    def get_document_version_by_id(self, version_id: UUID) -> DocumentVersion | None:
        """Fetch document version record by UUID [FR-4]."""
        return (
            self.db.query(DocumentVersion)
            .filter(DocumentVersion.id == version_id)
            .first()
        )

    def retrieve_document_payload(
        self, version_id: UUID
    ) -> tuple[DocumentVersion, bytes]:
        """Retrieve document version metadata and raw file payload from StoragePort [FR-4]."""
        version = self.get_document_version_by_id(version_id)
        if not version:
            raise FileNotFoundError(f"Document version {version_id} not found")

        if self.storage_port:
            content = self.storage_port.retrieve_document(version.file_path)
        else:
            content = b""

        return version, content

    def add_clause(
        self,
        contract_id: UUID,
        clause_type: str,
        title: str,
        body_text: str,
        is_highlighted: bool = False,
    ) -> ContractClause:
        """Add a clause entry to a contract [FR-1]."""
        contract = self.get_contract_by_id(contract_id)
        if not contract:
            raise ValueError(f"Contract {contract_id} not found")

        clause = ContractClause(
            contract_id=contract_id,
            clause_type=clause_type.strip(),
            title=title.strip(),
            body_text=body_text,
            is_highlighted=is_highlighted,
        )
        self.db.add(clause)
        self.db.commit()
        self.db.refresh(clause)
        return clause

    def get_contract_clauses(
        self, contract_id: UUID, highlighted_only: bool = False
    ) -> list[ContractClause]:
        """Get clauses for a contract, optionally filtering for highlighted key terms [FR-1]."""
        query = self.db.query(ContractClause).filter(
            ContractClause.contract_id == contract_id
        )
        if highlighted_only:
            query = query.filter(ContractClause.is_highlighted == True)
        return query.order_by(ContractClause.created_at.asc()).all()

    def add_sla(
        self,
        contract_id: UUID,
        metric_name: str,
        target_value: str,
        penalty_terms: str | None = None,
    ) -> ContractSLA:
        """Add a Service Level Agreement metric target to a contract [FR-1]."""
        contract = self.get_contract_by_id(contract_id)
        if not contract:
            raise ValueError(f"Contract {contract_id} not found")

        sla = ContractSLA(
            contract_id=contract_id,
            metric_name=metric_name.strip(),
            target_value=target_value.strip(),
            penalty_terms=penalty_terms.strip() if penalty_terms else None,
        )
        self.db.add(sla)
        self.db.commit()
        self.db.refresh(sla)
        return sla

    def get_contract_slas(self, contract_id: UUID) -> list[ContractSLA]:
        """Get SLA metrics defined for a contract [FR-1]."""
        return (
            self.db.query(ContractSLA)
            .filter(ContractSLA.contract_id == contract_id)
            .order_by(ContractSLA.created_at.asc())
            .all()
        )

    def evaluate_contract_slas(
        self,
        contract_id: UUID,
        metric_performance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Evaluate active SLAs, highlighted clauses, and key terms for an active agreement [FR-1]."""
        contract = self.get_contract_by_id(contract_id)
        if not contract:
            raise ValueError(f"Contract {contract_id} not found")

        slas = self.get_contract_slas(contract_id)
        highlighted_clauses = self.get_contract_clauses(
            contract_id, highlighted_only=True
        )

        is_active = contract.status in {
            ContractStatus.ACTIVE.value,
            ContractStatus.AMENDED.value,
            "ACTIVE",
            "AMENDED",
        }

        metric_performance = metric_performance or {}
        evaluated_slas: list[dict[str, Any]] = []

        for sla in slas:
            perf_val = metric_performance.get(sla.metric_name)
            status_eval = "COMPLIANT"
            if is_active and perf_val is not None:
                try:
                    target_num = float(str(sla.target_value).replace("%", "").strip())
                    actual_num = float(str(perf_val).replace("%", "").strip())
                    if actual_num < target_num:
                        status_eval = "BREACHED"
                except (ValueError, TypeError):
                    status_eval = "METRIC_RECORDED"

            evaluated_slas.append(
                {
                    "id": str(sla.id),
                    "metric_name": sla.metric_name,
                    "target_value": sla.target_value,
                    "penalty_terms": sla.penalty_terms,
                    "current_performance": perf_val,
                    "status": status_eval if is_active else "INACTIVE",
                }
            )

        return {
            "contract_id": str(contract.id),
            "title": contract.title,
            "contract_status": contract.status,
            "is_active": is_active,
            "slas": evaluated_slas,
            "highlighted_clauses": [
                {
                    "id": str(c.id),
                    "clause_type": c.clause_type,
                    "title": c.title,
                    "body_text": c.body_text,
                }
                for c in highlighted_clauses
            ],
        }
