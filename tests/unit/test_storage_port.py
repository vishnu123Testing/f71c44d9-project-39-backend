"""Unit tests for the StoragePort abstract interface and StoredDocumentResult DTO."""

import uuid

import pytest

from src.services.contract.models import ContractCategory
from src.services.contract.storage_port import StoragePort, StoredDocumentResult


class DummyStorageConnector(StoragePort):
    """Concrete test implementation of StoragePort for unit testing."""

    def __init__(self) -> None:
        self.storage: dict[str, bytes] = {}

    def store_document(
        self,
        contract_id: uuid.UUID,
        category_type: ContractCategory | str,
        version_number: int,
        file_name: str,
        content: bytes,
    ) -> StoredDocumentResult:
        category_str = (
            category_type.value if isinstance(category_type, ContractCategory) else str(category_type)
        )
        file_path = f"contracts/{contract_id}/{category_str}/v{version_number}_{file_name}"
        self.storage[file_path] = content
        return StoredDocumentResult(
            file_path=file_path,
            checksum="dummy_checksum_123",
            file_size=len(content),
            category_type=category_str,
            version_number=version_number,
        )

    def retrieve_document(self, file_path: str) -> bytes:
        if file_path not in self.storage:
            raise FileNotFoundError(f"Document not found at path: {file_path}")
        return self.storage[file_path]

    def delete_document(self, file_path: str) -> bool:
        if file_path in self.storage:
            del self.storage[file_path]
            return True
        return False

    def document_exists(self, file_path: str) -> bool:
        return file_path in self.storage


def test_cannot_instantiate_abstract_storage_port() -> None:
    """Verify that StoragePort cannot be instantiated directly without implementing abstract methods."""
    with pytest.raises(TypeError):
        StoragePort()  # type: ignore[abstract]


def test_stored_document_result_dto() -> None:
    """Verify StoredDocumentResult DTO instantiation and attribute accessibility."""
    result = StoredDocumentResult(
        file_path="contracts/123/SOW/v1_sow.pdf",
        checksum="abc123hash",
        file_size=2048,
        category_type="SOW",
        version_number=1,
    )
    assert result.file_path == "contracts/123/SOW/v1_sow.pdf"
    assert result.checksum == "abc123hash"
    assert result.file_size == 2048
    assert result.category_type == "SOW"
    assert result.version_number == 1


def test_dummy_storage_connector_lifecycle() -> None:
    """Verify storing, retrieving, existence checking, and deleting via StoragePort implementation."""
    connector = DummyStorageConnector()
    contract_id = uuid.uuid4()
    content = b"Contract test payload content"

    # Store document (FR-3 categorization, FR-4 versioning)
    result = connector.store_document(
        contract_id=contract_id,
        category_type=ContractCategory.CONTRACT,
        version_number=1,
        file_name="msa.pdf",
        content=content,
    )

    assert result.category_type == "Contract"
    assert result.version_number == 1
    assert result.file_size == len(content)
    assert connector.document_exists(result.file_path)

    # Retrieve document
    retrieved = connector.retrieve_document(result.file_path)
    assert retrieved == content

    # Delete document
    deleted = connector.delete_document(result.file_path)
    assert deleted is True
    assert not connector.document_exists(result.file_path)

    # Retrieve deleted document raises FileNotFoundError
    with pytest.raises(FileNotFoundError):
        connector.retrieve_document(result.file_path)
