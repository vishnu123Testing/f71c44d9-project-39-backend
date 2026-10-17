from datetime import date as date_type, datetime, timezone
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.contract import (
    Contract,
    ContractAmendment,
    ContractClause,
    ContractSLA,
    DocumentVersion,
    RenewalAlert,
    SignatureRequest,
)
from src.models.database import Base
from src.models.tenant import Tenant
from src.models.user import User


@pytest.fixture
def test_db_session() -> Session:
    """Fixture providing an isolated in-memory SQLite database session with created tables."""
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


def test_contract_tablenames() -> None:
    """Test that contract ORM models have expected table names."""
    assert Contract.__tablename__ == "contracts"
    assert ContractClause.__tablename__ == "contract_clauses"
    assert ContractSLA.__tablename__ == "contract_slas"
    assert DocumentVersion.__tablename__ == "document_versions"
    assert ContractAmendment.__tablename__ == "contract_amendments"
    assert SignatureRequest.__tablename__ == "signature_requests"
    assert RenewalAlert.__tablename__ == "renewal_alerts"


def test_contract_creation_and_defaults(test_db_session: Session) -> None:
    """Test creating a Contract record with default values."""
    contract = Contract(
        title="Master Services Agreement",
        category="Contract",
        counterparty="Acme Corp",
        start_on=date_type(2026, 1, 1),
        expires_on=date_type(2027, 1, 1),
    )
    test_db_session.add(contract)
    test_db_session.commit()
    test_db_session.refresh(contract)

    assert isinstance(contract.id, uuid.UUID)
    assert contract.title == "Master Services Agreement"
    assert contract.category == "Contract"
    assert contract.status == "DRAFT"
    assert contract.counterparty == "Acme Corp"
    assert contract.start_on == date_type(2026, 1, 1)
    assert contract.expires_on == date_type(2027, 1, 1)
    assert contract.parent_contract_id is None
    assert isinstance(contract.created_at, datetime)
    assert isinstance(contract.updated_at, datetime)
    assert repr(contract).startswith("<Contract(")


def test_parent_child_contract_relationship(test_db_session: Session) -> None:
    """Test parent contract and attached child SOW / Exhibit relationship [FR-10]."""
    parent = Contract(
        title="Master Services Agreement",
        category="Contract",
        counterparty="Acme Corp",
    )
    test_db_session.add(parent)
    test_db_session.commit()
    test_db_session.refresh(parent)

    child_sow = Contract(
        title="SOW 1 - Implementation",
        category="SOW",
        counterparty="Acme Corp",
        parent_contract_id=parent.id,
    )
    test_db_session.add(child_sow)
    test_db_session.commit()
    test_db_session.refresh(parent)
    test_db_session.refresh(child_sow)

    assert child_sow.parent_contract_id == parent.id
    assert child_sow.parent_contract == parent
    assert len(parent.child_contracts) == 1
    assert parent.child_contracts[0].title == "SOW 1 - Implementation"


def test_contract_clause_creation(test_db_session: Session) -> None:
    """Test creating ContractClause records linked to a Contract."""
    contract = Contract(
        title="NDA Agreement",
        category="NDA",
        counterparty="Global Corp",
    )
    test_db_session.add(contract)
    test_db_session.commit()

    clause = ContractClause(
        contract_id=contract.id,
        clause_type="CONFIDENTIALITY",
        title="Standard Confidentiality",
        body_text="All disclosures shall remain strictly confidential for 5 years.",
        is_highlighted=True,
    )
    test_db_session.add(clause)
    test_db_session.commit()
    test_db_session.refresh(contract)

    assert len(contract.clauses) == 1
    assert contract.clauses[0].clause_type == "CONFIDENTIALITY"
    assert contract.clauses[0].is_highlighted is True
    assert repr(clause).startswith("<ContractClause(")


def test_contract_sla_creation(test_db_session: Session) -> None:
    """Test creating ContractSLA records linked to a Contract."""
    contract = Contract(
        title="Cloud Hosting SOW",
        category="SOW",
        counterparty="Hosting LLC",
    )
    test_db_session.add(contract)
    test_db_session.commit()

    sla = ContractSLA(
        contract_id=contract.id,
        metric_name="Service Uptime",
        target_value="99.9%",
        penalty_terms="10% monthly service credit per 0.1% uptime drop.",
    )
    test_db_session.add(sla)
    test_db_session.commit()
    test_db_session.refresh(contract)

    assert len(contract.slas) == 1
    assert contract.slas[0].metric_name == "Service Uptime"
    assert contract.slas[0].target_value == "99.9%"
    assert repr(sla).startswith("<ContractSLA(")


def test_document_version_creation(test_db_session: Session) -> None:
    """Test creating DocumentVersion record linked to contract and uploading user [FR-3]."""
    tenant = Tenant(name="Acme Tenant")
    test_db_session.add(tenant)
    test_db_session.commit()

    user = User(
        email="uploader@example.com",
        password_hash="hashed_pw",
        tenant_id=tenant.tenant_id,
    )
    test_db_session.add(user)
    test_db_session.commit()

    contract = Contract(
        title="Exhibit A - Data Processing",
        category="Exhibit",
        counterparty="DataCorp",
    )
    test_db_session.add(contract)
    test_db_session.commit()

    doc_version = DocumentVersion(
        contract_id=contract.id,
        uploaded_by_id=user.user_id,
        category_type="Exhibit",
        version_number=1,
        file_path="/storage/contracts/exhibit_a_v1.pdf",
        checksum="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    )
    test_db_session.add(doc_version)
    test_db_session.commit()
    test_db_session.refresh(contract)

    assert len(contract.document_versions) == 1
    assert contract.document_versions[0].category_type == "Exhibit"
    assert contract.document_versions[0].version_number == 1
    assert contract.document_versions[0].uploaded_by == user
    assert repr(doc_version).startswith("<DocumentVersion(")


def test_contract_amendment_creation(test_db_session: Session) -> None:
    """Test creating ContractAmendment audit trail record [FR-12]."""
    tenant = Tenant(name="Amendment Tenant")
    test_db_session.add(tenant)
    test_db_session.commit()

    user = User(
        email="author@example.com",
        password_hash="hashed_pw",
        tenant_id=tenant.tenant_id,
    )
    test_db_session.add(user)
    test_db_session.commit()

    contract = Contract(
        title="Vendor Agreement",
        category="Contract",
        counterparty="Vendor Inc",
        status="ACTIVE",
    )
    test_db_session.add(contract)
    test_db_session.commit()

    amendment = ContractAmendment(
        contract_id=contract.id,
        author_id=user.user_id,
        sequence_number=1,
        summary="Extended termination notice window from 30 to 60 days.",
    )
    test_db_session.add(amendment)
    test_db_session.commit()
    test_db_session.refresh(contract)

    assert len(contract.amendments) == 1
    assert contract.amendments[0].sequence_number == 1
    assert contract.amendments[0].author == user
    assert repr(amendment).startswith("<ContractAmendment(")


def test_signature_request_creation(test_db_session: Session) -> None:
    """Test creating SignatureRequest record linked to contract and signatory user."""
    tenant = Tenant(name="Signature Tenant")
    test_db_session.add(tenant)
    test_db_session.commit()

    user = User(
        email="signatory@example.com",
        password_hash="hashed_pw",
        tenant_id=tenant.tenant_id,
    )
    test_db_session.add(user)
    test_db_session.commit()

    contract = Contract(
        title="Consulting Agreement",
        category="Contract",
        counterparty="Consultant Co",
        status="PENDING_SIGNATURE",
    )
    test_db_session.add(contract)
    test_db_session.commit()

    sig_request = SignatureRequest(
        contract_id=contract.id,
        signatory_id=user.user_id,
        status="EXECUTED",
        signature_digest="a1b2c3d4e5f67890",
        executed_at=datetime.now(timezone.utc),
    )
    test_db_session.add(sig_request)
    test_db_session.commit()
    test_db_session.refresh(contract)

    assert len(contract.signature_requests) == 1
    assert contract.signature_requests[0].status == "EXECUTED"
    assert contract.signature_requests[0].signatory == user
    assert repr(sig_request).startswith("<SignatureRequest(")


def test_renewal_alert_creation(test_db_session: Session) -> None:
    """Test creating RenewalAlert record linked to contract."""
    contract = Contract(
        title="Software Subscription",
        category="Contract",
        counterparty="SaaS Corp",
        status="ACTIVE",
    )
    test_db_session.add(contract)
    test_db_session.commit()

    alert = RenewalAlert(
        contract_id=contract.id,
        threshold_days=90,
    )
    test_db_session.add(alert)
    test_db_session.commit()
    test_db_session.refresh(contract)

    assert len(contract.renewal_alerts) == 1
    assert contract.renewal_alerts[0].threshold_days == 90
    assert repr(alert).startswith("<RenewalAlert(")
