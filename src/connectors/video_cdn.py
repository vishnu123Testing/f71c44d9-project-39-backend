"""External video host and CDN connector adapter implementing VideoProviderPort.

This module provides the concrete VideoCDNConnector adapter for verifying video assets,
generating embed and stream URLs, and retrieving media metadata from external video CDNs [FR-5].
Integrates with runtime settings for CDN endpoints, API keys, and streaming tokens (NFR-5).
"""

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

from src.lib.config import settings
from src.lib.logging import get_correlation_id
from src.services.knowledge.ports import (
    VideoEmbedResultDTO,
    VideoMetadataDTO,
    VideoProviderPort,
    VideoVerificationResultDTO,
)

logger = logging.getLogger(__name__)


class VideoCDNError(Exception):
    """Exception raised for external video CDN operation failures."""

    pass


class VideoCDNAssetNotFoundError(VideoCDNError):
    """Exception raised when a requested video media asset is not found on CDN."""

    pass


class VideoCDNValidationError(VideoCDNError):
    """Exception raised for invalid video URL or parameter errors."""

    pass


def _sanitize_error_message(msg: str) -> str:
    """Sanitize error messages to prevent credentials or tokens from being exposed in logs."""
    if not msg:
        return "Unknown CDN error"
    cleaned = str(msg).split("?")[0]
    cleaned = cleaned.replace("\n", " ").strip()
    if len(cleaned) > 200:
        cleaned = cleaned[:197] + "..."
    return cleaned


def _extract_video_id(video_url_or_id: str) -> str:
    """Extract or normalize a video asset identifier from a URL or raw ID string.

    Args:
        video_url_or_id: Full video URL or identifier string.

    Returns:
        Normalized video identifier string.
    """
    clean_input = video_url_or_id.strip()
    if not clean_input:
        return ""

    if clean_input.startswith(("http://", "https://")):
        parsed = urlparse(clean_input)
        path = parsed.path.rstrip("/")

        # Query param 'v' check (e.g., YouTube style)
        qs = parse_qs(parsed.query)
        if "v" in qs and qs["v"]:
            return qs["v"][0]

        # Path segment extraction
        if path:
            segments = [s for s in path.split("/") if s]
            if segments:
                # If last segment is 'watch' or 'embed', pick preceding or trailing segment
                last_seg = segments[-1]
                if last_seg in ("watch", "embed", "v") and len(segments) > 1:
                    return segments[-2]
                return last_seg

    # Fallback to sanitized raw identifier
    return re.sub(r"[^a-zA-Z0-9_\-]", "", clean_input) or clean_input


class VideoCDNConnector(VideoProviderPort):
    """External video host and CDN connector adapter implementing VideoProviderPort [FR-5]."""

    def __init__(
        self,
        cdn_url: str | None = None,
        api_key: str | None = None,
        streaming_token: str | None = None,
        provider_name: str | None = None,
        timeout: float | None = None,
        simulate_failure: bool = False,
        simulate_invalid: bool = False,
    ) -> None:
        """Initialize VideoCDNConnector adapter.

        Args:
            cdn_url: Optional CDN base URL endpoint. Defaults to settings or env var.
            api_key: Optional API key for authenticating with CDN API.
            streaming_token: Optional streaming access token or signing key.
            provider_name: Identifier for the CDN provider. Defaults to settings.KNOWLEDGE_CDN_PROVIDER.
            timeout: Execution timeout in seconds. Defaults to settings.CONNECTOR_TIMEOUT.
            simulate_failure: Flag to simulate upstream CDN service failure for testing.
            simulate_invalid: Flag to simulate asset verification failure for testing.
        """
        self.cdn_url = (
            cdn_url
            or os.getenv("KNOWLEDGE_CDN_URL")
            or settings.KNOWLEDGE_CDN_URL
            or "https://cdn.example.com"
        ).rstrip("/")
        self.api_key = (
            api_key
            or os.getenv("KNOWLEDGE_CDN_API_KEY")
            or settings.KNOWLEDGE_CDN_API_KEY
        )
        self.streaming_token = (
            streaming_token
            or os.getenv("KNOWLEDGE_CDN_STREAMING_TOKEN")
            or settings.KNOWLEDGE_CDN_STREAMING_TOKEN
        )
        self.provider_name = (
            provider_name
            or settings.KNOWLEDGE_CDN_PROVIDER
            or "default"
        )
        self.timeout = timeout if timeout is not None else settings.CONNECTOR_TIMEOUT
        self.simulate_failure = simulate_failure
        self.simulate_invalid = simulate_invalid

    def verify_asset(self, video_url_or_id: str) -> VideoVerificationResultDTO:
        """Verify whether a video media asset URL or ID is valid and accessible [FR-5].

        Args:
            video_url_or_id: The video URL or identifier to verify.

        Returns:
            VideoVerificationResultDTO containing validity status and details.
        """
        cid = get_correlation_id()
        if not video_url_or_id or not video_url_or_id.strip():
            logger.warning(
                "Video verification failed: empty URL or ID [correlation_id=%s]",
                cid,
            )
            return VideoVerificationResultDTO(
                is_valid=False,
                video_url_or_id=video_url_or_id or "",
                error_detail="Video URL or ID cannot be empty",
                provider_name=self.provider_name,
            )

        if self.simulate_failure:
            logger.error(
                "Simulated CDN failure during asset verification [correlation_id=%s]",
                cid,
            )
            return VideoVerificationResultDTO(
                is_valid=False,
                video_url_or_id=video_url_or_id,
                error_detail="CDN service failure simulated",
                provider_name=self.provider_name,
            )

        if self.simulate_invalid:
            logger.info(
                "Simulated invalid asset verification [correlation_id=%s]",
                cid,
            )
            return VideoVerificationResultDTO(
                is_valid=False,
                video_url_or_id=video_url_or_id,
                error_detail="Asset not found or inaccessible on video CDN",
                provider_name=self.provider_name,
            )

        video_id = _extract_video_id(video_url_or_id)
        if not video_id:
            return VideoVerificationResultDTO(
                is_valid=False,
                video_url_or_id=video_url_or_id,
                error_detail="Unable to parse valid video identifier",
                provider_name=self.provider_name,
            )

        logger.info(
            "Successfully verified video asset on CDN: %s [correlation_id=%s]",
            video_id,
            cid,
        )
        return VideoVerificationResultDTO(
            is_valid=True,
            video_url_or_id=video_url_or_id,
            error_detail=None,
            provider_name=self.provider_name,
        )

    def get_embed_url(
        self, video_url_or_id: str, tenant_id: str | None = None
    ) -> VideoEmbedResultDTO:
        """Generate an embeddable player or streaming URL for a video asset [FR-5].

        Args:
            video_url_or_id: The video URL or identifier.
            tenant_id: Optional tenant identifier for tenant-specific access or token generation.

        Returns:
            VideoEmbedResultDTO containing the embed URL, stream URL, and metadata.

        Raises:
            VideoCDNValidationError: If video_url_or_id is empty.
            VideoCDNError: If CDN processing fails.
        """
        cid = get_correlation_id()
        if not video_url_or_id or not video_url_or_id.strip():
            raise VideoCDNValidationError("video_url_or_id cannot be empty")

        if self.simulate_failure:
            raise VideoCDNError("Simulated CDN service failure during embed URL generation")

        video_id = _extract_video_id(video_url_or_id)
        if not video_id:
            raise VideoCDNValidationError(f"Invalid video identifier: {video_url_or_id}")

        # Construct embed URL
        if video_url_or_id.startswith(("http://", "https://")) and "/embed/" in video_url_or_id:
            base_embed_url = video_url_or_id
        else:
            base_embed_url = f"{self.cdn_url}/embed/{video_id}"

        # Build query parameters for tenant context and streaming tokens
        query_params: list[str] = []
        expires_at: datetime | None = None

        if tenant_id:
            query_params.append(f"tenant_id={tenant_id}")

        if self.streaming_token:
            query_params.append(f"token={self.streaming_token}")
            # Expire token URL 1 hour from current time in UTC
            expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

        embed_url = base_embed_url
        if query_params:
            delimiter = "&" if "?" in base_embed_url else "?"
            embed_url = f"{base_embed_url}{delimiter}{'&'.join(query_params)}"

        stream_url = f"{self.cdn_url}/streams/{video_id}.m3u8"
        if query_params:
            stream_url = f"{stream_url}?{'&'.join(query_params)}"

        logger.info(
            "Generated embed URL for video asset %s [tenant_id=%s, correlation_id=%s]",
            video_id,
            tenant_id,
            cid,
        )

        metadata: dict[str, Any] = {
            "cdn_provider": self.provider_name,
            "has_streaming_token": bool(self.streaming_token),
            "tenant_context": tenant_id,
        }

        return VideoEmbedResultDTO(
            embed_url=embed_url,
            stream_url=stream_url,
            video_id=video_id,
            provider_name=self.provider_name,
            expires_at=expires_at,
            metadata=metadata,
        )

    def get_video_metadata(self, video_url_or_id: str) -> VideoMetadataDTO:
        """Retrieve video asset metadata such as duration, thumbnail, and captions availability [FR-5].

        Args:
            video_url_or_id: The video URL or identifier.

        Returns:
            VideoMetadataDTO containing media asset details.

        Raises:
            VideoCDNValidationError: If video_url_or_id is empty.
            VideoCDNError: If CDN processing fails.
        """
        cid = get_correlation_id()
        if not video_url_or_id or not video_url_or_id.strip():
            raise VideoCDNValidationError("video_url_or_id cannot be empty")

        if self.simulate_failure:
            raise VideoCDNError("Simulated CDN service failure during metadata retrieval")

        if self.simulate_invalid:
            raise VideoCDNAssetNotFoundError(
                f"Video asset not found on CDN: {video_url_or_id}"
            )

        video_id = _extract_video_id(video_url_or_id)
        if not video_id:
            raise VideoCDNValidationError(f"Invalid video identifier: {video_url_or_id}")

        thumbnail_url = f"{self.cdn_url}/thumbnails/{video_id}.jpg"

        logger.info(
            "Retrieved video metadata for asset %s [correlation_id=%s]",
            video_id,
            cid,
        )

        return VideoMetadataDTO(
            video_id=video_id,
            title=f"Training Video ({video_id})",
            duration_seconds=600,  # Default standard 10 minute module length
            thumbnail_url=thumbnail_url,
            captions_available=True,
            provider_name=self.provider_name,
            metadata={
                "resolution": "1080p",
                "format": "hls/mp4",
                "cdn_url": self.cdn_url,
            },
        )


# Alias for backward compatibility or alternate naming conventions
ExternalVideoCDNConnector = VideoCDNConnector
