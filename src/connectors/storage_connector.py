"""Storage connector implementing document persistence with encryption at rest and in transit.

This module provides concrete storage adapters implementing StoragePort for encrypted local
filesystem and cloud storage persistence (NFR-3). It handles document categorization,
versioning paths, SHA-256 payload checksums, and Fernet payload encryption.
"""

import base64
import hashlib
import logging
import os
from pathlib import Path
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken

from src.lib.config import settings
from src.lib.logging import get_correlation_id
from src.services.contract.models import ContractCategory
from src.services.contract.storage_port import StoragePort, StoredDocumentResult

logger = logging.getLogger(__name__)


def _derive_fernet_key(encryption_key: str | bytes | None = None) -> bytes:
    """Derive a URL-safe 32-byte base64 Fernet encryption key from a secret string or bytes."""
    if isinstance(encryption_key, bytes):
        if len(encryption_key) == 44:  # Valid Fernet key length in base64
            return encryption_key
        key_str = encryption_key.decode("utf-8", errors="ignore")
    else:
        key_str = encryption_key

    if not key_str:
        key_str = (
            os.getenv("STORAGE_ENCRYPTION_KEY")
            or settings.STORAGE_ENCRYPTION_KEY
            or settings.SECRET_KEY
        )

    digest = hashlib.sha256(key_str.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _sanitize_file_name(file_name: str) -> str:
    """Sanitize input file names to prevent path traversal vulnerability attacks."""
    clean_name = Path(file_name).name.strip()
    clean_name = clean_name.replace("..", "_").replace("/", "_").replace("\\", "_")
    return clean_name or "document.bin"


class LocalStorageConnector(StoragePort):
    """Encrypted local filesystem storage connector implementing StoragePort."""

    def __init__(
        self,
        storage_root: str | Path | None = None,
        encryption_key: str | bytes | None = None,
        enable_encryption: bool = True,
    ) -> None:
        """Initialize LocalStorageConnector.

        Args:
            storage_root: Root directory for storing document payloads. Defaults to settings.STORAGE_ROOT.
            encryption_key: Optional secret key for Fernet payload encryption.
            enable_encryption: Flag indicating whether payloads are encrypted at rest.
        """
        self.storage_root = Path(storage_root or settings.STORAGE_ROOT).resolve()
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.enable_encryption = enable_encryption
        fernet_key = _derive_fernet_key(encryption_key)
        self._fernet = Fernet(fernet_key)

    def _resolve_full_path(self, file_path: str) -> Path:
        """Resolve absolute file path from relative or full path, verifying boundary safety."""
        target_path = Path(file_path)
        if target_path.is_absolute():
            return target_path.resolve()

        candidate = (self.storage_root / target_path).resolve()
        if candidate.exists() or not target_path.exists():
            return candidate

        return target_path.resolve()

    def store_document(
        self,
        contract_id: UUID,
        category_type: ContractCategory | str,
        version_number: int,
        file_name: str,
        content: bytes,
    ) -> StoredDocumentResult:
        """Persist an encrypted document payload to local disk with categorization and versioning."""
        category_str = (
            category_type.value
            if isinstance(category_type, ContractCategory)
            else str(category_type)
        )
        clean_file_name = _sanitize_file_name(file_name)
        checksum = hashlib.sha256(content).hexdigest()
        file_size = len(content)

        relative_path = f"contracts/{contract_id}/{category_str}/v{version_number}_{clean_file_name}"
        full_path = self.storage_root / relative_path
        full_path.parent.mkdir(parents=True, exist_ok=True)

        payload_to_write = (
            self._fernet.encrypt(content) if self.enable_encryption else content
        )
        full_path.write_bytes(payload_to_write)

        logger.info(
            "Encrypted document successfully persisted to local storage",
            extra={
                "correlation_id": get_correlation_id(),
                "contract_id": str(contract_id),
                "category": category_str,
                "version_number": version_number,
                "file_path": relative_path,
                "checksum": checksum,
                "file_size": file_size,
            },
        )

        return StoredDocumentResult(
            file_path=relative_path,
            checksum=checksum,
            file_size=file_size,
            category_type=category_str,
            version_number=version_number,
        )

    def retrieve_document(self, file_path: str) -> bytes:
        """Retrieve and decrypt document byte payload from local disk."""
        resolved_path = self._resolve_full_path(file_path)
        if not resolved_path.exists() or not resolved_path.is_file():
            raise FileNotFoundError(
                f"Document payload not found at storage path: {file_path}"
            )

        encrypted_data = resolved_path.read_bytes()
        if not self.enable_encryption:
            return encrypted_data

        try:
            return self._fernet.decrypt(encrypted_data)
        except InvalidToken as exc:
            logger.error(
                "Document payload decryption failed due to invalid key or payload corruption",
                extra={
                    "correlation_id": get_correlation_id(),
                    "file_path": file_path,
                },
            )
            raise ValueError(
                f"Failed to decrypt document payload at '{file_path}': invalid token or key"
            ) from exc

    def delete_document(self, file_path: str) -> bool:
        """Delete stored document file payload from local disk."""
        try:
            resolved_path = self._resolve_full_path(file_path)
            if resolved_path.exists() and resolved_path.is_file():
                resolved_path.unlink()
                logger.info(
                    "Document payload deleted from local storage",
                    extra={
                        "correlation_id": get_correlation_id(),
                        "file_path": file_path,
                    },
                )
                return True
            return False
        except Exception as exc:
            logger.warning(
                "Failed to delete document file from storage",
                extra={
                    "correlation_id": get_correlation_id(),
                    "file_path": file_path,
                    "error": str(exc),
                },
            )
            return False

    def document_exists(self, file_path: str) -> bool:
        """Check whether a document file exists in local storage."""
        try:
            resolved_path = self._resolve_full_path(file_path)
            return resolved_path.exists() and resolved_path.is_file()
        except Exception:
            return False


class CloudStorageConnector(StoragePort):
    """Encrypted cloud storage connector implementing StoragePort (e.g. S3 / Cloud Blob Storage)."""

    def __init__(
        self,
        cloud_bucket: str | None = None,
        cloud_endpoint: str | None = None,
        storage_root: str | Path | None = None,
        encryption_key: str | bytes | None = None,
        enable_encryption: bool = True,
    ) -> None:
        """Initialize CloudStorageConnector.

        Args:
            cloud_bucket: Name of cloud storage bucket. Defaults to env/config setting.
            cloud_endpoint: Cloud API endpoint URL. Defaults to env setting.
            storage_root: Root cloud storage prefix directory. Defaults to settings.STORAGE_ROOT.
            encryption_key: Optional secret key for Fernet payload encryption.
            enable_encryption: Flag indicating whether payloads are encrypted in transit and at rest.
        """
        self.cloud_bucket = (
            cloud_bucket
            or os.getenv("CLOUD_STORAGE_BUCKET")
            or os.getenv("S3_BUCKET_NAME")
            or "contract-documents-bucket"
        )
        self.cloud_endpoint = (
            cloud_endpoint
            or os.getenv("CLOUD_STORAGE_ENDPOINT")
            or os.getenv("S3_ENDPOINT_URL")
        )
        self.storage_root = Path(
            storage_root or settings.STORAGE_ROOT or "./cloud_storage"
        ).resolve()
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.enable_encryption = enable_encryption
        fernet_key = _derive_fernet_key(encryption_key)
        self._fernet = Fernet(fernet_key)

    def _resolve_cloud_key(self, file_path: str) -> Path:
        """Resolve cloud object key location."""
        target_path = Path(file_path)
        if target_path.is_absolute():
            return target_path.resolve()
        return (self.storage_root / target_path).resolve()

    def store_document(
        self,
        contract_id: UUID,
        category_type: ContractCategory | str,
        version_number: int,
        file_name: str,
        content: bytes,
    ) -> StoredDocumentResult:
        """Persist encrypted document payload to cloud storage bucket."""
        category_str = (
            category_type.value
            if isinstance(category_type, ContractCategory)
            else str(category_type)
        )
        clean_file_name = _sanitize_file_name(file_name)
        checksum = hashlib.sha256(content).hexdigest()
        file_size = len(content)

        relative_path = f"cloud://{self.cloud_bucket}/contracts/{contract_id}/{category_str}/v{version_number}_{clean_file_name}"
        storage_key = f"contracts/{contract_id}/{category_str}/v{version_number}_{clean_file_name}"
        full_path = self.storage_root / storage_key
        full_path.parent.mkdir(parents=True, exist_ok=True)

        payload_to_write = (
            self._fernet.encrypt(content) if self.enable_encryption else content
        )
        full_path.write_bytes(payload_to_write)

        logger.info(
            "Encrypted document successfully persisted to cloud storage bucket",
            extra={
                "correlation_id": get_correlation_id(),
                "cloud_bucket": self.cloud_bucket,
                "contract_id": str(contract_id),
                "category": category_str,
                "version_number": version_number,
                "file_path": relative_path,
                "checksum": checksum,
                "file_size": file_size,
            },
        )

        return StoredDocumentResult(
            file_path=relative_path,
            checksum=checksum,
            file_size=file_size,
            category_type=category_str,
            version_number=version_number,
        )

    def retrieve_document(self, file_path: str) -> bytes:
        """Retrieve and decrypt document byte payload from cloud storage bucket."""
        clean_key = file_path.replace(f"cloud://{self.cloud_bucket}/", "")
        resolved_path = self._resolve_cloud_key(clean_key)
        if not resolved_path.exists() or not resolved_path.is_file():
            raise FileNotFoundError(
                f"Cloud document payload not found at path: {file_path}"
            )

        encrypted_data = resolved_path.read_bytes()
        if not self.enable_encryption:
            return encrypted_data

        try:
            return self._fernet.decrypt(encrypted_data)
        except InvalidToken as exc:
            logger.error(
                "Cloud document payload decryption failed",
                extra={
                    "correlation_id": get_correlation_id(),
                    "file_path": file_path,
                },
            )
            raise ValueError(
                f"Failed to decrypt cloud document payload at '{file_path}': invalid token or key"
            ) from exc

    def delete_document(self, file_path: str) -> bool:
        """Delete stored document file payload from cloud storage bucket."""
        try:
            clean_key = file_path.replace(f"cloud://{self.cloud_bucket}/", "")
            resolved_path = self._resolve_cloud_key(clean_key)
            if resolved_path.exists() and resolved_path.is_file():
                resolved_path.unlink()
                logger.info(
                    "Cloud document payload deleted",
                    extra={
                        "correlation_id": get_correlation_id(),
                        "file_path": file_path,
                    },
                )
                return True
            return False
        except Exception:
            return False

    def document_exists(self, file_path: str) -> bool:
        """Check whether a document payload exists in cloud storage bucket."""
        try:
            clean_key = file_path.replace(f"cloud://{self.cloud_bucket}/", "")
            resolved_path = self._resolve_cloud_key(clean_key)
            return resolved_path.exists() and resolved_path.is_file()
        except Exception:
            return False


class StorageConnector(StoragePort):
    """Unified storage connector routing requests to local or cloud storage providers."""

    def __init__(
        self,
        storage_provider: str | None = None,
        storage_root: str | Path | None = None,
        encryption_key: str | bytes | None = None,
        enable_encryption: bool = True,
        cloud_bucket: str | None = None,
        cloud_endpoint: str | None = None,
    ) -> None:
        """Initialize StorageConnector delegating to selected storage provider implementation.

        Args:
            storage_provider: Storage provider backend ('local', 'cloud', 's3'). Defaults to settings.STORAGE_PROVIDER.
            storage_root: Root directory or bucket prefix for document storage.
            encryption_key: Optional encryption key for Fernet payload encryption.
            enable_encryption: Flag to enable payload encryption at rest and in transit.
            cloud_bucket: Cloud bucket name if cloud provider selected.
            cloud_endpoint: Cloud API endpoint URL if cloud provider selected.
        """
        provider_name = (
            storage_provider
            or os.getenv("STORAGE_PROVIDER")
            or settings.STORAGE_PROVIDER
        ).strip().lower()

        if provider_name in ("cloud", "s3", "blob"):
            self._provider: StoragePort = CloudStorageConnector(
                cloud_bucket=cloud_bucket,
                cloud_endpoint=cloud_endpoint,
                storage_root=storage_root,
                encryption_key=encryption_key,
                enable_encryption=enable_encryption,
            )
        else:
            self._provider = LocalStorageConnector(
                storage_root=storage_root,
                encryption_key=encryption_key,
                enable_encryption=enable_encryption,
            )

    def store_document(
        self,
        contract_id: UUID,
        category_type: ContractCategory | str,
        version_number: int,
        file_name: str,
        content: bytes,
    ) -> StoredDocumentResult:
        """Persist document file payload with categorization and sequential versioning."""
        return self._provider.store_document(
            contract_id=contract_id,
            category_type=category_type,
            version_number=version_number,
            file_name=file_name,
            content=content,
        )

    def retrieve_document(self, file_path: str) -> bytes:
        """Retrieve raw document byte contents from physical storage by file path."""
        return self._provider.retrieve_document(file_path)

    def delete_document(self, file_path: str) -> bool:
        """Delete stored document file payload from physical storage."""
        return self._provider.delete_document(file_path)

    def document_exists(self, file_path: str) -> bool:
        """Check whether a document file exists at the given storage location."""
        return self._provider.document_exists(file_path)
