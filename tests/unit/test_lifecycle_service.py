"""Unit tests for ContractLifecycleService domain logic, versioning, SLA evaluation, and search filtering."""

from datetime import date as date_type, datetime, timezone
import tempfile
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.connectors.storage_connector import LocalStorageConnector
from src.models.contract import Contract, ContractClause, ContractSLA, DocumentVersion
from src.models.database import Base
from src.services.contract.lifecycle_service import (
    ContractLifecycleService,
    to_domain_entity,
)
from src.services.contract.models import (
    ContractCategory,
    ContractDomain,
    ContractEventType,
    ContractStatus,
)


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
def temp_storage_connector():
    """Fixture providing LocalStorageConnector pointing to a temporary directory."""
    with tempfile.TemporaryDirectory() as temp_dir:
        connector = LocalStorageConnector(storage_root=temp_dir, enable_encryption=True)
        yield connector


def test_create_contract(test_db_session: Session) -> None:
    """Test creating a contract with clauses and SLAs [FR-3, FR-12]."""
    service = ContractLifecycleService(db=test_db_session)

    clauses_input = [
        {
            "clause_type": "Indemnification",
            "title": "Standard Mutual Indemnity",
            "body_text": "Each party shall indemnify...",
            "is_highlighted": True,
        }
    ]
    slas_input = [
        {
            "metric_name": "Uptime Guarantee",
            "target_value": "99.9%",
            "penalty_terms": "10% service credit",
        }
    ]

    contract = service.create_contract(
        title="Master Services Agreement",
        category=ContractCategory.CONTRACT,
        counterparty="Acme Corp",
        start_on=date_type(2026, 1, 1),
        expires_on=date_type(2027, 1, 1),
        clauses=clauses_input,
        slas=slas_input,
    )

    assert isinstance(contract.id, UUID)
    assert contract.title == "Master Services Agreement"
    assert contract.category == "Contract"
    assert contract.status == "DRAFT"
    assert contract.counterparty == "Acme Corp"
    assert len(contract.clauses) == 1
    assert contract.clauses[0].is_highlighted is True
    assert len(contract.slas) == 1
    assert contract.slas[0].metric_name == "Uptime Guarantee"

    # Verify pure domain entity mapping
    domain_entity = to_domain_entity(contract)
    assert isinstance(domain_entity, ContractDomain)
    assert domain_entity.title == "Master Services Agreement"
    assert domain_entity.category == ContractCategory.CONTRACT


def test_create_child_contract(test_db_session: Session) -> None:
    """Test creating a child contract SOW linked to a parent contract [FR-10]."""
    service = ContractLifecycleService(db=test_db_session)

    parent = service.create_contract(
        title="Parent MSA",
        category=ContractCategory.CONTRACT,
        counterparty="Globex Corp",
    )

    child = service.create_contract(
        title="Statement of Work 1",
        category=ContractCategory.SOW,
        counterparty="Globex Corp",
        parent_contract_id=parent.id,
    )

    assert child.parent_contract_id == parent.id


def test_update_contract_and_status(test_db_session: Session) -> None:
    """Test updating contract attributes and status transitions."""
    service = ContractLifecycleService(db=test_db_session)

    contract = service.create_contract(
        title="Initial Title",
        category=ContractCategory.NDA,
        counterparty="Initech",
    )

    updated = service.update_contract(
        contract_id=contract.id,
        title="Updated Title",
        status=ContractStatus.ACTIVE,
    )

    assert updated.title == "Updated Title"
    assert updated.status == "ACTIVE"


def test_sequential_document_version_control(
    test_db_session: Session, temp_storage_connector: LocalStorageConnector
) -> None:
    """Test sequential version control (v1, v2, v3) on document uploads [FR-4]."""
    service = ContractLifecycleService(
        db=test_db_session, storage_port=temp_storage_connector
    )

    contract = service.create_contract(
        title="Vendor Agreement",
        category=ContractCategory.CONTRACT,
        counterparty="Stark Industries",
    )

    v1 = service.upload_document_version(
        contract_id=contract.id,
        category_type=ContractCategory.CONTRACT,
        file_name="vendor_contract_v1.pdf",
        content=b"Version 1 Content Payload",
    )

    assert v1.version_number == 1
    assert v1.category_type == "Contract"

    v2 = service.upload_document_version(
        contract_id=contract.id,
        category_type=ContractCategory.SOW,
        file_name="sow_attachment_v2.pdf",
        content=b"Version 2 SOW Content Payload",
    )

    assert v2.version_number == 2

    versions = service.get_document_versions(contract.id)
    assert len(versions) == 2
    assert [v.version_number for v in versions] == [1, 2]

    # Verify payload retrieval
    doc, content = service.retrieve_document_payload(v1.id)
    assert doc.id == v1.id
    assert content == b"Version 1 Content Payload"


def test_search_contracts_filtering(test_db_session: Session) -> None:
    """Test multi-criteria search repository filtering [FR-11]."""
    service = ContractLifecycleService(db=test_db_session)

    c1 = service.create_contract(
        title="Cloud Hosting MSA",
        category=ContractCategory.CONTRACT,
        counterparty="Amazon Web Services",
        expires_on=date_type(2026, 6, 30),
    )
    service.update_contract(c1.id, status=ContractStatus.ACTIVE)

    c2 = service.create_contract(
        title="Consulting SOW",
        category=ContractCategory.SOW,
        counterparty="McKinsey",
        expires_on=date_type(2026, 12, 31),
    )

    c3 = service.create_contract(
        title="Mutual NDA",
        category=ContractCategory.NDA,
        counterparty="Amazon Logistics",
        expires_on=date_type(2028, 1, 1),
    )

    # Filter by category
    results, count = service.search_contracts(category=ContractCategory.CONTRACT)
    assert count == 1
    assert results[0].id == c1.id

    # Filter by status
    results, count = service.search_contracts(status=ContractStatus.ACTIVE)
    assert count == 1
    assert results[0].id == c1.id

    # Filter by counterparty
    results, count = service.search_contracts(counterparty="Amazon")
    assert count == 2

    # Filter by expiration date
    results, count = service.search_contracts(expires_before=date_type(2026, 7, 1))
    assert count == 1
    assert results[0].id == c1.id

    # Search query
    results, count = service.search_contracts(search_query="Hosting")
    assert count == 1
    assert results[0].id == c1.id


def test_evaluate_contract_slas(test_db_session: Session) -> None:
    """Test evaluating SLAs and highlighted clauses for active agreements [FR-1]."""
    service = ContractLifecycleService(db=test_db_session)

    contract = service.create_contract(
        title="SLA Contract",
        category=ContractCategory.CONTRACT,
        counterparty="Telecom Co",
    )
    service.add_clause(
        contract_id=contract.id,
        clause_type="Support",
        title="Response Time",
        body_text="1 hour response time for critical issues",
        is_highlighted=True,
    )
    service.add_sla(
        contract_id=contract.id,
        metric_name="Uptime Guarantee",
        target_value="99.9%",
        penalty_terms="5% credit",
    )

    # Draft status evaluation -> inactive
    eval_draft = service.evaluate_contract_slas(
        contract.id, metric_performance={"Uptime Guarantee": "99.5%"}
    )
    assert eval_draft["is_active"] is False
    assert eval_draft["slas"][0]["status"] == "INACTIVE"
    assert len(eval_draft["highlighted_clauses"]) == 1

    # Transition to ACTIVE
    service.update_contract(contract.id, status=ContractStatus.ACTIVE)

    # Evaluated with compliant performance
    eval_compliant = service.evaluate_contract_slas(
        contract.id, metric_performance={"Uptime Guarantee": "99.95%"}
    )
    assert eval_compliant["is_active"] is True
    assert eval_compliant["slas"][0]["status"] == "COMPLIANT"

    # Evaluated with breached performance
    eval_breached = service.evaluate_contract_slas(
        contract.id, metric_performance={"Uptime Guarantee": "98.0%"}
    )
    assert eval_breached["slas"][0]["status"] == "BREACHED"


def test_delete_contract(
    test_db_session: Session, temp_storage_connector: LocalStorageConnector
) -> None:
    """Test deleting contract and cleaning up stored documents."""
    service = ContractLifecycleService(
        db=test_db_session, storage_port=temp_storage_connector
    )

    contract = service.create_contract(
        title="Temporary Agreement",
        category=ContractCategory.NDA,
        counterparty="Temp Inc",
    )
    v1 = service.upload_document_version(
        contract_id=contract.id,
        category_type=ContractCategory.NDA,
        file_name="temp.pdf",
        content=b"Temp payload",
    )

    assert temp_storage_connector.document_exists(v1.file_path) is True

    success = service.delete_contract(contract.id)
    assert success is True
    assert service.get_contract_by_id(contract.id) is None
    assert temp_storage_connector.document_exists(v1.file_path) is False
