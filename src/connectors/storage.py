"""Storage connector port definition and blob storage adapters for support operations attachments.

This module provides the StoragePort abstract interface and concrete storage adapters
(LocalStorageAdapter, BlobStorageAdapter, StorageConnector) for storing, retrieving, deleting,
and validating file attachments associated with support tickets [FR-3, NFR-2, NFR-4, NFR-5].
"""

from abc import ABC, abstractmethod
import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import logging
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from src.lib.config import settings
from src.lib.logging import get_correlation_id

logger = logging.getLogger(__name__)


@dataclass
class FileValidationResult:
    """Result object for boundary file attachment validation [FR-3, NFR-2]."""

    is_valid: bool
    error_message: str | None = None


@dataclass
class StoredFileResult:
    """Standardized metadata result for stored file attachments."""

    storage_key: str
    filename: str
    file_size_bytes: int
    mime_type: str
    checksum: str
    storage_path: str
    uploaded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _derive_fernet_key(encryption_key: str | bytes | None = None) -> bytes:
    """Derive a URL-safe 32-byte base64 Fernet encryption key from a secret string or bytes."""
    if isinstance(encryption_key, bytes):
        if len(encryption_key) == 44:
            return encryption_key
        key_str = encryption_key.decode("utf-8", errors="ignore")
    else:
        key_str = encryption_key

    if not key_str:
        key_str = (
            os.getenv("STORAGE_ENCRYPTION_KEY")
            or getattr(settings, "STORAGE_ENCRYPTION_KEY", None)
            or settings.SECRET_KEY
        )

    digest = hashlib.sha256(key_str.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def sanitize_filename(filename: str) -> str:
    """Sanitize input file name to prevent path traversal vulnerability attacks."""
    if not filename:
        return "attachment.bin"
    clean_name = Path(filename).name.strip()
    clean_name = clean_name.replace("..", "_").replace("/", "_").replace("\\", "_")
    return clean_name or "attachment.bin"


class StoragePort(ABC):
    """Abstract storage port contract for support attachment operations [NFR-4]."""

    @abstractmethod
    def validate_file(
        self,
        filename: str,
        file_size_bytes: int,
        mime_type: str | None = None,
        content: bytes | None = None,
    ) -> FileValidationResult:
        """Validate file attachment size limits, extension, and MIME type at entry point [FR-3, NFR-2].

        Args:
            filename: Name of the file attachment.
            file_size_bytes: File size in bytes.
            mime_type: Optional MIME content type.
            content: Optional raw content bytes.

        Returns:
            FileValidationResult indicating if the attachment is valid or why it was rejected.
        """
        ...

    @abstractmethod
    def store_file(
        self,
        filename: str,
        content: bytes,
        mime_type: str | None = None,
        subfolder: str | None = None,
    ) -> StoredFileResult:
        """Store a file attachment and return metadata.

        Args:
            filename: Original file name.
            content: Raw byte contents.
            mime_type: Optional MIME content type.
            subfolder: Optional subfolder directory.

        Returns:
            StoredFileResult containing storage key, checksum, size, and location path.
        """
        ...

    @abstractmethod
    def retrieve_file(self, storage_key: str) -> bytes:
        """Retrieve raw file attachment byte contents by storage key.

        Args:
            storage_key: Resolved unique storage key or path.

        Returns:
            Raw byte contents of the file.

        Raises:
            FileNotFoundError: If the file is not found.
            ValueError: If decryption or reading fails.
        """
        ...

    @abstractmethod
    def delete_file(self, storage_key: str) -> bool:
        """Delete a stored file attachment by storage key.

        Args:
            storage_key: Resolved unique storage key or path.

        Returns:
            True if successfully deleted, False if not found.
        """
        ...

    @abstractmethod
    def file_exists(self, storage_key: str) -> bool:
        """Check whether a file exists for the given storage key.

        Args:
            storage_key: Resolved unique storage key or path.

        Returns:
            True if file exists, False otherwise.
        """
        ...


class BaseStorageAdapter(StoragePort):
    """Base storage adapter implementing boundary validation rules [FR-3, NFR-2]."""

    def __init__(
        self,
        max_file_size_bytes: int | None = None,
        allowed_extensions: list[str] | None = None,
        allowed_mime_types: list[str] | None = None,
    ) -> None:
        """Initialize BaseStorageAdapter with boundary validation parameters."""
        self.max_file_size_bytes = (
            max_file_size_bytes
            if max_file_size_bytes is not None
            else getattr(settings, "MAX_ATTACHMENT_SIZE_BYTES", 10 * 1024 * 1024)
        )
        self.allowed_extensions = (
            allowed_extensions
            if allowed_extensions is not None
            else getattr(
                settings,
                "ALLOWED_ATTACHMENT_EXTENSIONS",
                [".pdf", ".docx", ".doc", ".txt", ".png", ".jpg", ".jpeg"],
            )
        )
        self.allowed_mime_types = (
            allowed_mime_types
            if allowed_mime_types is not None
            else getattr(
                settings,
                "ALLOWED_ATTACHMENT_MIME_TYPES",
                [
                    "application/pdf",
                    "application/msword",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "text/plain",
                    "image/png",
                    "image/jpeg",
                ],
            )
        )

    def validate_file(
        self,
        filename: str,
        file_size_bytes: int,
        mime_type: str | None = None,
        content: bytes | None = None,
    ) -> FileValidationResult:
        """Validate file attachment bounds: size limit, allowed extensions, MIME types [FR-3, NFR-2]."""
        if content is not None and file_size_bytes == 0:
            file_size_bytes = len(content)

        if file_size_bytes > self.max_file_size_bytes:
            max_mb = self.max_file_size_bytes / (1024 * 1024)
            err = f"File size ({file_size_bytes} bytes) exceeds maximum limit of {max_mb:.1f} MB."
            logger.warning(
                "Attachment size limit validation failed",
                extra={
                    "correlation_id": get_correlation_id(),
                    "filename": filename,
                    "file_size_bytes": file_size_bytes,
                },
            )
            return FileValidationResult(is_valid=False, error_message=err)

        ext = Path(filename).suffix.lower()
        if ext not in [e.lower() for e in self.allowed_extensions]:
            allowed_str = ", ".join(self.allowed_extensions)
            err = f"File extension '{ext}' is not permitted. Allowed extensions: {allowed_str}"
            logger.warning(
                "Attachment extension validation failed",
                extra={
                    "correlation_id": get_correlation_id(),
                    "filename": filename,
                    "extension": ext,
                },
            )
            return FileValidationResult(is_valid=False, error_message=err)

        if mime_type and self.allowed_mime_types:
            clean_mime = mime_type.split(";")[0].strip().lower()
            allowed_mimes = [m.lower() for m in self.allowed_mime_types]
            if clean_mime not in allowed_mimes:
                allowed_str = ", ".join(self.allowed_mime_types)
                err = f"MIME type '{clean_mime}' is not permitted. Allowed MIME types: {allowed_str}"
                logger.warning(
                    "Attachment MIME type validation failed",
                    extra={
                        "correlation_id": get_correlation_id(),
                        "filename": filename,
                        "mime_type": clean_mime,
                    },
                )
                return FileValidationResult(is_valid=False, error_message=err)

        return FileValidationResult(is_valid=True)


class LocalStorageAdapter(BaseStorageAdapter):
    """Local filesystem storage adapter implementing StoragePort."""

    def __init__(
        self,
        storage_root: str | Path | None = None,
        max_file_size_bytes: int | None = None,
        allowed_extensions: list[str] | None = None,
        allowed_mime_types: list[str] | None = None,
        encryption_key: str | bytes | None = None,
        enable_encryption: bool = False,
    ) -> None:
        """Initialize LocalStorageAdapter.

        Args:
            storage_root: Root directory for storage files.
            max_file_size_bytes: Maximum allowed file size limit.
            allowed_extensions: Allowed file extension list.
            allowed_mime_types: Allowed MIME content types list.
            encryption_key: Key for Fernet payload encryption.
            enable_encryption: Flag for payload encryption at rest.
        """
        super().__init__(
            max_file_size_bytes=max_file_size_bytes,
            allowed_extensions=allowed_extensions,
            allowed_mime_types=allowed_mime_types,
        )
        root_dir = (
            storage_root
            or getattr(settings, "SUPPORT_ATTACHMENTS_DIR", None)
            or getattr(settings, "STORAGE_ROOT", "./storage")
        )
        self.storage_root = Path(root_dir).resolve()
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.enable_encryption = enable_encryption
        fernet_key = _derive_fernet_key(encryption_key)
        self._fernet = Fernet(fernet_key)

    def _resolve_path(self, storage_key: str) -> Path:
        """Resolve storage key to safe absolute path within storage root."""
        clean_key = storage_key.lstrip("/").lstrip("\\")
        target_path = Path(clean_key)
        if target_path.is_absolute():
            return target_path.resolve()
        return (self.storage_root / target_path).resolve()

    def store_file(
        self,
        filename: str,
        content: bytes,
        mime_type: str | None = None,
        subfolder: str | None = None,
    ) -> StoredFileResult:
        """Store a file attachment on disk after boundary validation [FR-3, NFR-2]."""
        validation = self.validate_file(
            filename=filename,
            file_size_bytes=len(content),
            mime_type=mime_type,
            content=content,
        )
        if not validation.is_valid:
            raise ValueError(f"Attachment boundary validation error: {validation.error_message}")

        clean_name = sanitize_filename(filename)
        resolved_mime = mime_type or "application/octet-stream"
        checksum = hashlib.sha256(content).hexdigest()
        unique_prefix = f"{uuid4().hex[:12]}_{clean_name}"

        if subfolder:
            rel_folder = Path(subfolder.strip("/").strip("\\"))
            storage_key = str(rel_folder / unique_prefix)
        else:
            storage_key = unique_prefix

        target_file = self._resolve_path(storage_key)
        target_file.parent.mkdir(parents=True, exist_ok=True)

        payload_to_write = self._fernet.encrypt(content) if self.enable_encryption else content
        target_file.write_bytes(payload_to_write)

        logger.info(
            "Attachment stored successfully",
            extra={
                "correlation_id": get_correlation_id(),
                "filename": clean_name,
                "storage_key": storage_key,
                "file_size_bytes": len(content),
            },
        )

        return StoredFileResult(
            storage_key=storage_key,
            filename=clean_name,
            file_size_bytes=len(content),
            mime_type=resolved_mime,
            checksum=checksum,
            storage_path=str(target_file),
        )

    def retrieve_file(self, storage_key: str) -> bytes:
        """Retrieve raw bytes for a file attachment by storage key."""
        target_file = self._resolve_path(storage_key)
        if not target_file.exists():
            raise FileNotFoundError(f"Attachment file not found for key: {storage_key}")

        raw_bytes = target_file.read_bytes()
        if self.enable_encryption:
            try:
                return self._fernet.decrypt(raw_bytes)
            except InvalidToken as err:
                raise ValueError(f"Failed to decrypt file payload for key: {storage_key}") from err

        return raw_bytes

    def delete_file(self, storage_key: str) -> bool:
        """Delete a stored file attachment from local disk."""
        target_file = self._resolve_path(storage_key)
        if target_file.exists():
            target_file.unlink()
            return True
        return False

    def file_exists(self, storage_key: str) -> bool:
        """Check if file exists on disk."""
        return self._resolve_path(storage_key).exists()


class BlobStorageAdapter(BaseStorageAdapter):
    """Blob storage adapter simulating cloud object storage container persistence."""

    def __init__(
        self,
        bucket_name: str | None = None,
        max_file_size_bytes: int | None = None,
        allowed_extensions: list[str] | None = None,
        allowed_mime_types: list[str] | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
    ) -> None:
        """Initialize BlobStorageAdapter for blob storage operations."""
        super().__init__(
            max_file_size_bytes=max_file_size_bytes,
            allowed_extensions=allowed_extensions,
            allowed_mime_types=allowed_mime_types,
        )
        self.bucket_name = (
            bucket_name
            or getattr(settings, "STORAGE_BUCKET_NAME", None)
            or "support-attachments"
        )
        self.access_key = access_key or getattr(settings, "STORAGE_ACCESS_KEY", None)
        self.secret_key = secret_key or getattr(settings, "STORAGE_SECRET_KEY", None)
        self._blob_container: dict[str, dict[str, Any]] = {}

    def store_file(
        self,
        filename: str,
        content: bytes,
        mime_type: str | None = None,
        subfolder: str | None = None,
    ) -> StoredFileResult:
        """Store a file in blob container storage after boundary validation [FR-3, NFR-2]."""
        validation = self.validate_file(
            filename=filename,
            file_size_bytes=len(content),
            mime_type=mime_type,
            content=content,
        )
        if not validation.is_valid:
            raise ValueError(f"Attachment boundary validation error: {validation.error_message}")

        clean_name = sanitize_filename(filename)
        resolved_mime = mime_type or "application/octet-stream"
        checksum = hashlib.sha256(content).hexdigest()
        unique_prefix = f"{uuid4().hex[:12]}_{clean_name}"

        folder_prefix = subfolder.strip("/") + "/" if subfolder else ""
        storage_key = f"{self.bucket_name}/{folder_prefix}{unique_prefix}"

        self._blob_container[storage_key] = {
            "content": content,
            "filename": clean_name,
            "mime_type": resolved_mime,
            "checksum": checksum,
            "uploaded_at": datetime.now(timezone.utc),
        }

        logger.info(
            "Attachment stored in blob storage container",
            extra={
                "correlation_id": get_correlation_id(),
                "bucket": self.bucket_name,
                "storage_key": storage_key,
                "file_size_bytes": len(content),
            },
        )

        return StoredFileResult(
            storage_key=storage_key,
            filename=clean_name,
            file_size_bytes=len(content),
            mime_type=resolved_mime,
            checksum=checksum,
            storage_path=f"blob://{storage_key}",
        )

    def retrieve_file(self, storage_key: str) -> bytes:
        """Retrieve raw file content from blob container storage."""
        if storage_key not in self._blob_container:
            raise FileNotFoundError(f"Blob attachment not found for key: {storage_key}")
        return self._blob_container[storage_key]["content"]

    def delete_file(self, storage_key: str) -> bool:
        """Delete a file from blob container storage."""
        if storage_key in self._blob_container:
            del self._blob_container[storage_key]
            return True
        return False

    def file_exists(self, storage_key: str) -> bool:
        """Check if file exists in blob container storage."""
        return storage_key in self._blob_container


class StorageConnector(StoragePort):
    """Unified Storage Connector routing operations to configured storage provider."""

    def __init__(
        self,
        provider: str | None = None,
        adapter: StoragePort | None = None,
    ) -> None:
        """Initialize StorageConnector.

        Args:
            provider: Selected storage provider ("local", "blob", "s3").
            adapter: Explicit storage adapter instance.
        """
        if adapter is not None:
            self.adapter = adapter
        else:
            prov = (
                provider
                or getattr(settings, "STORAGE_PROVIDER", "local")
                or "local"
            ).lower()
            if prov in ("blob", "s3", "azure", "gcs"):
                self.adapter = BlobStorageAdapter()
            else:
                self.adapter = LocalStorageAdapter()

    def validate_file(
        self,
        filename: str,
        file_size_bytes: int,
        mime_type: str | None = None,
        content: bytes | None = None,
    ) -> FileValidationResult:
        """Delegate file validation to configured adapter."""
        return self.adapter.validate_file(
            filename=filename,
            file_size_bytes=file_size_bytes,
            mime_type=mime_type,
            content=content,
        )

    def store_file(
        self,
        filename: str,
        content: bytes,
        mime_type: str | None = None,
        subfolder: str | None = None,
    ) -> StoredFileResult:
        """Delegate file storing to configured adapter."""
        return self.adapter.store_file(
            filename=filename,
            content=content,
            mime_type=mime_type,
            subfolder=subfolder,
        )

    def retrieve_file(self, storage_key: str) -> bytes:
        """Delegate file retrieval to configured adapter."""
        return self.adapter.retrieve_file(storage_key)

    def delete_file(self, storage_key: str) -> bool:
        """Delegate file deletion to configured adapter."""
        return self.adapter.delete_file(storage_key)

    def file_exists(self, storage_key: str) -> bool:
        """Delegate file existence check to configured adapter."""
        return self.adapter.file_exists(storage_key)


def get_storage_adapter(
    provider: str | None = None,
    storage_root: str | Path | None = None,
) -> StoragePort:
    """Factory function returning a StoragePort adapter instance.

    Args:
        provider: Storage provider type ("local", "blob").
        storage_root: Optional storage root directory path.

    Returns:
        Configured StoragePort instance.
    """
    prov = (
        provider
        or getattr(settings, "STORAGE_PROVIDER", "local")
        or "local"
    ).lower()

    if prov in ("blob", "s3", "azure", "gcs"):
        return BlobStorageAdapter()
    return LocalStorageAdapter(storage_root=storage_root)
