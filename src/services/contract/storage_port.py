"""Abstract document persistence storage port definition for contract lifecycle management.

This module defines the abstract port interface that decouples physical document file
storage adapters (e.g., local filesystem, cloud blob storage connectors) from core domain services.
Supports document categorization (FR-3: Contract, SOW, NDA, Exhibit) and sequential version control (FR-4).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from uuid import UUID

from src.services.contract.models import ContractCategory


@dataclass
class StoredDocumentResult:
    """Standardized DTO representing the persisted document metadata result."""

    file_path: str
    checksum: str
    file_size: int
    category_type: str
    version_number: int


class StoragePort(ABC):
    """Abstract port contract for document persistence operations."""

    @abstractmethod
    def store_document(
        self,
        contract_id: UUID,
        category_type: ContractCategory | str,
        version_number: int,
        file_name: str,
        content: bytes,
    ) -> StoredDocumentResult:
        """Persist a document file payload with categorization and sequential versioning.

        Args:
            contract_id: Unique UUID identifier of the contract.
            category_type: Categorization type (Contract, SOW, NDA, Exhibit).
            version_number: Incremental sequential version number for version control.
            file_name: Original file name or extension.
            content: Raw byte contents of the document.

        Returns:
            StoredDocumentResult containing the resolved file path, checksum, and size.
        """
        ...

    @abstractmethod
    def retrieve_document(self, file_path: str) -> bytes:
        """Retrieve raw document byte contents from physical storage by file path.

        Args:
            file_path: Resolved storage location path of the document.

        Returns:
            Raw byte payload of the requested document version.

        Raises:
            FileNotFoundError: If the document payload is not found at the file_path.
        """
        ...

    @abstractmethod
    def delete_document(self, file_path: str) -> bool:
        """Delete a stored document file payload from physical storage.

        Args:
            file_path: Resolved storage location path of the document.

        Returns:
            True if the document was successfully deleted, False if not found.
        """
        ...

    @abstractmethod
    def document_exists(self, file_path: str) -> bool:
        """Check whether a document file exists at the given storage location.

        Args:
            file_path: Resolved storage location path of the document.

        Returns:
            True if document file exists, False otherwise.
        """
        ...
