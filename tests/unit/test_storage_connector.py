"""Unit tests for storage connector implementations (LocalStorageConnector, CloudStorageConnector, StorageConnector)."""

import hashlib
import uuid
from pathlib import Path

import pytest

from src.connectors.storage_connector import (
    CloudStorageConnector,
    LocalStorageConnector,
    StorageConnector,
    _derive_fernet_key,
    _sanitize_file_name,
)
from src.services.contract.models import ContractCategory


def test_derive_fernet_key_deterministic() -> None:
    """Verify key derivation generates valid 44-byte URL-safe base64 key deterministically."""
    key1 = _derive_fernet_key("my-secret-key-123")
    key2 = _derive_fernet_key("my-secret-key-123")
    key3 = _derive_fernet_key("different-key")

    assert len(key1) == 44
    assert key1 == key2
    assert key1 != key3


def test_sanitize_file_name_prevents_path_traversal() -> None:
    """Verify file name sanitization strips path traversal sequences."""
    assert _sanitize_file_name("../../etc/passwd") == "passwd"
    assert _sanitize_file_name("sub/dir/contract.pdf") == "contract.pdf"
    assert _sanitize_file_name("   msa.docx  ") == "msa.docx"
    assert _sanitize_file_name("") == "document.bin"


def test_local_storage_connector_lifecycle(tmp_path: Path) -> None:
    """Verify local storage document store, retrieve, existence check, encryption at rest, and deletion."""
    connector = LocalStorageConnector(
        storage_root=tmp_path,
        encryption_key="test-encryption-key-321",
        enable_encryption=True,
    )
    contract_id = uuid.uuid4()
    raw_content = b"Confidential Contract Content Details"

    # Store document
    result = connector.store_document(
        contract_id=contract_id,
        category_type=ContractCategory.CONTRACT,
        version_number=1,
        file_name="msa_agreement.pdf",
        content=raw_content,
    )

    expected_checksum = hashlib.sha256(raw_content).hexdigest()
    assert result.checksum == expected_checksum
    assert result.file_size == len(raw_content)
    assert result.category_type == "Contract"
    assert result.version_number == 1
    assert "contracts/" in result.file_path

    # Verify document exists
    assert connector.document_exists(result.file_path) is True

    # Verify physical file on disk is encrypted (at rest encryption)
    physical_file = tmp_path / result.file_path
    assert physical_file.exists()
    disk_bytes = physical_file.read_bytes()
    assert disk_bytes != raw_content

    # Retrieve document and verify decryption
    retrieved = connector.retrieve_document(result.file_path)
    assert retrieved == raw_content

    # Delete document
    assert connector.delete_document(result.file_path) is True
    assert connector.document_exists(result.file_path) is False


def test_local_storage_connector_invalid_key_raises_value_error(tmp_path: Path) -> None:
    """Verify retrieving a document with an invalid decryption key raises ValueError."""
    connector1 = LocalStorageConnector(
        storage_root=tmp_path,
        encryption_key="key-one",
        enable_encryption=True,
    )
    contract_id = uuid.uuid4()
    raw_content = b"Sensitive Payload Data"

    result = connector1.store_document(
        contract_id=contract_id,
        category_type=ContractCategory.NDA,
        version_number=1,
        file_name="nda.pdf",
        content=raw_content,
    )

    connector2 = LocalStorageConnector(
        storage_root=tmp_path,
        encryption_key="wrong-key",
        enable_encryption=True,
    )

    with pytest.raises(ValueError, match="Failed to decrypt document payload"):
        connector2.retrieve_document(result.file_path)


def test_local_storage_connector_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    """Verify retrieving a non-existent file raises FileNotFoundError."""
    connector = LocalStorageConnector(storage_root=tmp_path)
    with pytest.raises(FileNotFoundError, match="Document payload not found"):
        connector.retrieve_document("non/existent/path.pdf")


def test_cloud_storage_connector_lifecycle(tmp_path: Path) -> None:
    """Verify cloud storage connector persistence, retrieval, and deletion."""
    connector = CloudStorageConnector(
        cloud_bucket="test-contract-bucket",
        storage_root=tmp_path,
        encryption_key="cloud-secret-key",
        enable_encryption=True,
    )
    contract_id = uuid.uuid4()
    raw_content = b"Cloud Stored SOW Document Payload"

    result = connector.store_document(
        contract_id=contract_id,
        category_type=ContractCategory.SOW,
        version_number=2,
        file_name="sow_v2.pdf",
        content=raw_content,
    )

    assert "cloud://test-contract-bucket/" in result.file_path
    assert result.category_type == "SOW"
    assert result.version_number == 2
    assert connector.document_exists(result.file_path) is True

    # Retrieve and check
    retrieved = connector.retrieve_document(result.file_path)
    assert retrieved == raw_content

    # Delete
    assert connector.delete_document(result.file_path) is True
    assert connector.document_exists(result.file_path) is False


def test_unified_storage_connector_factory(tmp_path: Path) -> None:
    """Verify unified StorageConnector routes to local or cloud based on provider config."""
    local_storage = StorageConnector(
        storage_provider="local",
        storage_root=tmp_path / "local",
        encryption_key="unified-key",
    )
    cloud_storage = StorageConnector(
        storage_provider="cloud",
        storage_root=tmp_path / "cloud",
        cloud_bucket="unified-bucket",
        encryption_key="unified-key",
    )

    contract_id = uuid.uuid4()
    content = b"Unified Connector Payload"

    res_local = local_storage.store_document(
        contract_id=contract_id,
        category_type=ContractCategory.EXHIBIT,
        version_number=1,
        file_name="exhibit_a.pdf",
        content=content,
    )
    assert "contracts/" in res_local.file_path
    assert local_storage.retrieve_document(res_local.file_path) == content

    res_cloud = cloud_storage.store_document(
        contract_id=contract_id,
        category_type=ContractCategory.EXHIBIT,
        version_number=1,
        file_name="exhibit_a.pdf",
        content=content,
    )
    assert "cloud://unified-bucket/" in res_cloud.file_path
    assert cloud_storage.retrieve_document(res_cloud.file_path) == content
