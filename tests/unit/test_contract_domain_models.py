"""Unit tests for contract domain entities, dataclasses, and lifecycle event payloads."""

from datetime import date as date_type, datetime, timezone
import uuid

from src.services.contract.models import (
    ContractAmendedEventPayload,
    ContractAmendmentDomain,
    ContractCategory,
    ContractClauseDomain,
    ContractCreationEventPayload,
    ContractDomain,
    ContractEventType,
    ContractLifecycleEventPayload,
    ContractRenewalAlertEventPayload,
    ContractSignedEventPayload,
    ContractSLADomain,
    ContractStatus,
    DocumentVersionDomain,
    RenewalAlertDomain,
    SignatureRequestDomain,
    SignatureStatus,
)


def test_contract_enums() -> None:
    assert ContractCategory.CONTRACT.value == "Contract"
    assert ContractCategory.SOW.value == "SOW"

    assert ContractStatus.DRAFT.value == "DRAFT"
    assert ContractStatus.ACTIVE.value == "ACTIVE"

    assert SignatureStatus.PENDING.value == "PENDING"
    assert SignatureStatus.EXECUTED.value == "EXECUTED"

    assert ContractEventType.CREATION.value == "CREATION"
    assert ContractEventType.SIGNATURE_EXECUTED.value == "SIGNATURE_EXECUTED"


def test_contract_domain_creation() -> None:
    contract_id = uuid.uuid4()
    start_on = date_type(2026, 1, 1)
    expires_on = date_type(2027, 1, 1)

    clause = ContractClauseDomain(
        id=uuid.uuid4(),
        contract_id=contract_id,
        clause_type="Indemnification",
        title="Standard Indemnity",
        body_text="Counterparty shall indemnify...",
        is_highlighted=True,
    )

    sla = ContractSLADomain(
        id=uuid.uuid4(),
        contract_id=contract_id,
        metric_name="Uptime Guarantee",
        target_value="99.9%",
        penalty_terms="10% credit for downtime",
    )

    doc_version = DocumentVersionDomain(
        id=uuid.uuid4(),
        contract_id=contract_id,
        category_type="Contract",
        version_number=1,
        file_path="/storage/contracts/doc_v1.pdf",
        checksum="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    )

    amendment = ContractAmendmentDomain(
        id=uuid.uuid4(),
        contract_id=contract_id,
        sequence_number=1,
        summary="Updated payment terms to Net 30",
    )

    sig_request = SignatureRequestDomain(
        id=uuid.uuid4(),
        contract_id=contract_id,
        status=SignatureStatus.PENDING,
    )

    alert = RenewalAlertDomain(
        id=uuid.uuid4(),
        contract_id=contract_id,
        threshold_days=90,
    )

    contract = ContractDomain(
        id=contract_id,
        title="Master Services Agreement",
        category=ContractCategory.CONTRACT,
        counterparty="Acme Corp",
        status=ContractStatus.DRAFT,
        start_on=start_on,
        expires_on=expires_on,
        clauses=[clause],
        slas=[sla],
        document_versions=[doc_version],
        amendments=[amendment],
        signature_requests=[sig_request],
        renewal_alerts=[alert],
    )

    assert contract.id == contract_id
    assert contract.title == "Master Services Agreement"
    assert contract.category == ContractCategory.CONTRACT
    assert len(contract.clauses) == 1
    assert contract.clauses[0].is_highlighted is True
    assert len(contract.slas) == 1
    assert contract.slas[0].target_value == "99.9%"
    assert len(contract.document_versions) == 1
    assert len(contract.amendments) == 1
    assert len(contract.signature_requests) == 1
    assert len(contract.renewal_alerts) == 1


def test_contract_lifecycle_event_payload_serialization() -> None:
    contract_id = uuid.uuid4()
    user_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    payload = ContractLifecycleEventPayload(
        event_type=ContractEventType.CREATION,
        contract_id=contract_id,
        performed_by_id=user_id,
        timestamp=now,
        correlation_id="corr-12345",
        metadata={"category": "MSA", "version": 1},
    )

    data = payload.to_dict()

    assert data["event_type"] == "CREATION"
    assert data["contract_id"] == str(contract_id)
    assert data["performed_by_id"] == str(user_id)
    assert data["timestamp"] == now.isoformat()
    assert data["correlation_id"] == "corr-12345"
    assert data["metadata"] == {"category": "MSA", "version": 1}


def test_specific_event_payloads() -> None:
    contract_id = uuid.uuid4()
    user_id = uuid.uuid4()
    sig_req_id = uuid.uuid4()
    amendment_id = uuid.uuid4()
    alert_id = uuid.uuid4()

    # Creation
    creation_event = ContractCreationEventPayload(
        contract_id=contract_id,
        title="Statement of Work",
        category="SOW",
        counterparty="TechCorp",
        performed_by_id=user_id,
        start_on=date_type(2026, 6, 1),
        expires_on=date_type(2027, 6, 1),
    )
    creation_dict = creation_event.to_dict()
    assert creation_dict["event_type"] == "CREATION"
    assert creation_dict["metadata"]["title"] == "Statement of Work"
    assert creation_dict["metadata"]["start_on"] == "2026-06-01"

    # Signed
    signed_event = ContractSignedEventPayload(
        contract_id=contract_id,
        signature_request_id=sig_req_id,
        signatory_id=user_id,
        signature_digest="sha256:abc123digest",
    )
    signed_dict = signed_event.to_dict()
    assert signed_dict["event_type"] == "SIGNATURE_EXECUTED"
    assert signed_dict["metadata"]["signature_request_id"] == str(sig_req_id)
    assert signed_dict["metadata"]["signature_digest"] == "sha256:abc123digest"

    # Amended
    amended_event = ContractAmendedEventPayload(
        contract_id=contract_id,
        amendment_id=amendment_id,
        sequence_number=1,
        summary="Price adjustment",
        author_id=user_id,
    )
    amended_dict = amended_event.to_dict()
    assert amended_dict["event_type"] == "AMENDMENT_ADDED"
    assert amended_dict["metadata"]["sequence_number"] == 1
    assert amended_dict["metadata"]["summary"] == "Price adjustment"

    # Renewal Alert
    renewal_event = ContractRenewalAlertEventPayload(
        contract_id=contract_id,
        alert_id=alert_id,
        threshold_days=60,
        expires_on=date_type(2027, 6, 1),
    )
    renewal_dict = renewal_event.to_dict()
    assert renewal_dict["event_type"] == "RENEWAL_ALERT_DISPATCHED"
    assert renewal_dict["metadata"]["threshold_days"] == 60
