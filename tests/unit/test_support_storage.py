"""Unit tests for support storage connector port and blob storage adapters."""

import hashlib
from pathlib import Path
import pytest

from src.connectors.storage import (
    BlobStorageAdapter,
    LocalStorageAdapter,
    StorageConnector,
    get_storage_adapter,
    sanitize_filename,
)


def test_sanitize_filename_prevents_path_traversal() -> None:
    """Verify filename sanitization strips dangerous path traversal sequences."""
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("sub/dir/log.txt") == "log.txt"
    assert sanitize_filename("  report.pdf  ") == "report.pdf"
    assert sanitize_filename("") == "attachment.bin"


def test_local_storage_adapter_boundary_validation() -> None:
    """Verify size limits, extension rules, and MIME type validation [FR-3, NFR-2]."""
    adapter = LocalStorageAdapter(
        max_file_size_bytes=1000,
        allowed_extensions=[".pdf", ".png"],
        allowed_mime_types=["application/pdf", "image/png"],
    )

    # Valid file
    res = adapter.validate_file("document.pdf", 500, "application/pdf")
    assert res.is_valid is True
    assert res.error_message is None

    # Oversized file
    res_large = adapter.validate_file("huge.pdf", 5000, "application/pdf")
    assert res_large.is_valid is False
    assert "exceeds maximum limit" in res_large.error_message

    # Disallowed extension
    res_ext = adapter.validate_file("script.exe", 100, "application/pdf")
    assert res_ext.is_valid is False
    assert "extension '.exe' is not permitted" in res_ext.error_message

    # Disallowed MIME type
    res_mime = adapter.validate_file("doc.pdf", 100, "application/x-msdownload")
    assert res_mime.is_valid is False
    assert "MIME type 'application/x-msdownload' is not permitted" in res_mime.error_message


def test_local_storage_adapter_lifecycle(tmp_path: Path) -> None:
    """Verify storing, checking existence, retrieving, and deleting files on disk."""
    adapter = LocalStorageAdapter(
        storage_root=tmp_path,
        max_file_size_bytes=10000,
        allowed_extensions=[".txt", ".pdf"],
        allowed_mime_types=["text/plain", "application/pdf"],
        enable_encryption=False,
    )
    content = b"Sample support ticket attachment content log."

    # Store file
    result = adapter.store_file("debug.txt", content, mime_type="text/plain", subfolder="ticket_123")
    expected_checksum = hashlib.sha256(content).hexdigest()

    assert result.checksum == expected_checksum
    assert result.file_size_bytes == len(content)
    assert result.mime_type == "text/plain"
    assert "ticket_123" in result.storage_key

    # File exists
    assert adapter.file_exists(result.storage_key) is True

    # Retrieve file
    retrieved = adapter.retrieve_file(result.storage_key)
    assert retrieved == content

    # Delete file
    assert adapter.delete_file(result.storage_key) is True
    assert adapter.file_exists(result.storage_key) is False


def test_local_storage_adapter_encryption(tmp_path: Path) -> None:
    """Verify encryption at rest and decryption on retrieval."""
    adapter = LocalStorageAdapter(
        storage_root=tmp_path,
        max_file_size_bytes=10000,
        allowed_extensions=[".txt"],
        allowed_mime_types=["text/plain"],
        encryption_key="secret-test-key-999",
        enable_encryption=True,
    )
    raw_content = b"Confidential log data requiring encryption."

    result = adapter.store_file("secure.txt", raw_content, mime_type="text/plain")

    # Inspect physical disk contents to ensure encryption at rest
    physical_path = tmp_path / result.storage_key
    assert physical_path.exists()
    disk_bytes = physical_path.read_bytes()
    assert disk_bytes != raw_content

    # Decrypt on retrieve
    decrypted = adapter.retrieve_file(result.storage_key)
    assert decrypted == raw_content


def test_local_storage_adapter_invalid_validation_raises_value_error(tmp_path: Path) -> None:
    """Verify store_file raises ValueError if boundary validation fails."""
    adapter = LocalStorageAdapter(
        storage_root=tmp_path,
        max_file_size_bytes=100,
        allowed_extensions=[".pdf"],
    )
    large_payload = b"X" * 500

    with pytest.raises(ValueError, match="Attachment boundary validation error"):
        adapter.store_file("large.pdf", large_payload, mime_type="application/pdf")


def test_blob_storage_adapter_lifecycle() -> None:
    """Verify BlobStorageAdapter storing, checking existence, retrieving, and deleting."""
    adapter = BlobStorageAdapter(
        bucket_name="test-bucket",
        max_file_size_bytes=10000,
        allowed_extensions=[".png", ".jpg"],
        allowed_mime_types=["image/png", "image/jpeg"],
    )
    image_data = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"

    # Store in blob storage
    result = adapter.store_file("screenshot.png", image_data, mime_type="image/png", subfolder="logs")
    assert "test-bucket/logs/" in result.storage_key
    assert result.file_size_bytes == len(image_data)
    assert result.checksum == hashlib.sha256(image_data).hexdigest()

    # Check existence
    assert adapter.file_exists(result.storage_key) is True

    # Retrieve
    retrieved = adapter.retrieve_file(result.storage_key)
    assert retrieved == image_data

    # Delete
    assert adapter.delete_file(result.storage_key) is True
    assert adapter.file_exists(result.storage_key) is False


def test_blob_storage_adapter_missing_key_raises_file_not_found() -> None:
    """Verify retrieving non-existent blob key raises FileNotFoundError."""
    adapter = BlobStorageAdapter(bucket_name="test-bucket")
    with pytest.raises(FileNotFoundError, match="Blob attachment not found"):
        adapter.retrieve_file("test-bucket/nonexistent_file.png")


def test_storage_connector_routing() -> None:
    """Verify StorageConnector delegates correctly based on provider."""
    local_connector = StorageConnector(provider="local")
    assert isinstance(local_connector.adapter, LocalStorageAdapter)

    blob_connector = StorageConnector(provider="blob")
    assert isinstance(blob_connector.adapter, BlobStorageAdapter)


def test_get_storage_adapter_factory(tmp_path: Path) -> None:
    """Verify get_storage_adapter factory function."""
    adapter_local = get_storage_adapter(provider="local", storage_root=tmp_path)
    assert isinstance(adapter_local, LocalStorageAdapter)

    adapter_blob = get_storage_adapter(provider="s3")
    assert isinstance(adapter_blob, BlobStorageAdapter)
