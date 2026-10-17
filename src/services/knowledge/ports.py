"""Abstract video streaming provider port interface for knowledge repository.

This module defines the abstract port contract (VideoProviderPort) and associated DTOs
for video media verification, embed URL generation, and metadata retrieval (FR-5).
Decouples core business logic from external CDN and video hosting infrastructure.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class VideoMetadataDTO:
    """Standardized DTO representing metadata for a video streaming asset [FR-5]."""

    video_id: str
    title: str | None = None
    duration_seconds: int | None = None
    thumbnail_url: str | None = None
    captions_available: bool = False
    provider_name: str = "default"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class VideoEmbedResultDTO:
    """Standardized DTO representing an embeddable player or streaming URL result [FR-5]."""

    embed_url: str
    stream_url: str | None = None
    video_id: str = ""
    provider_name: str = "default"
    expires_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class VideoVerificationResultDTO:
    """Standardized DTO representing the outcome of verifying a video media asset [FR-5]."""

    is_valid: bool
    video_url_or_id: str
    error_detail: str | None = None
    provider_name: str = "default"


class VideoProviderPort(ABC):
    """Abstract port interface for video streaming provider integrations [FR-5]."""

    @abstractmethod
    def verify_asset(self, video_url_or_id: str) -> VideoVerificationResultDTO:
        """Verify whether a video media asset URL or ID is valid and accessible.

        Args:
            video_url_or_id: The video URL or identifier to verify.

        Returns:
            VideoVerificationResultDTO containing validity status and details.
        """
        ...

    @abstractmethod
    def get_embed_url(
        self, video_url_or_id: str, tenant_id: str | None = None
    ) -> VideoEmbedResultDTO:
        """Generate an embeddable player or streaming URL for a video asset.

        Args:
            video_url_or_id: The video URL or identifier.
            tenant_id: Optional tenant identifier for tenant-specific access or token generation.

        Returns:
            VideoEmbedResultDTO containing the embed URL, stream URL, and metadata.
        """
        ...

    @abstractmethod
    def get_video_metadata(self, video_url_or_id: str) -> VideoMetadataDTO:
        """Retrieve video asset metadata such as duration, thumbnail, and captions availability.

        Args:
            video_url_or_id: The video URL or identifier.

        Returns:
            VideoMetadataDTO containing media asset details.
        """
        ...
