"""Unit tests for External Video Host CDN connector (VideoCDNConnector).

Verifies asset verification, embed URL generation, tenant token handling,
metadata retrieval, and error/simulation branches [FR-5, NFR-5].
"""

from datetime import datetime, timezone

import pytest

from src.connectors.video_cdn import (
    ExternalVideoCDNConnector,
    VideoCDNConnector,
    VideoCDNError,
    VideoCDNValidationError,
    _extract_video_id,
    _sanitize_error_message,
)
from src.services.knowledge.ports import (
    VideoEmbedResultDTO,
    VideoMetadataDTO,
    VideoVerificationResultDTO,
)


def test_extract_video_id_variations():
    """Test video ID extraction from various URL shapes and raw strings."""
    assert _extract_video_id("") == ""
    assert _extract_video_id("   ") == ""
    assert _extract_video_id("v12345") == "v12345"
    assert _extract_video_id("https://cdn.example.com/embed/v12345") == "v12345"
    assert _extract_video_id("https://youtube.com/watch?v=abc987") == "abc987"
    assert _extract_video_id("https://vimeo.com/12345678") == "12345678"


def test_sanitize_error_message():
    """Test sanitization of error messages to avoid leaking sensitive parameters."""
    assert _sanitize_error_message("") == "Unknown CDN error"
    assert (
        _sanitize_error_message("Error at https://cdn.com/embed?token=secret123")
        == "Error at https://cdn.com/embed"
    )


def test_verify_asset_success():
    """Test successful verification of valid video asset URL/ID."""
    connector = VideoCDNConnector(cdn_url="https://cdn.test.com")
    result = connector.verify_asset("v12345")

    assert isinstance(result, VideoVerificationResultDTO)
    assert result.is_valid is True
    assert result.video_url_or_id == "v12345"
    assert result.error_detail is None
    assert result.provider_name == "default"


def test_verify_asset_empty():
    """Test asset verification with empty string."""
    connector = VideoCDNConnector()
    result = connector.verify_asset("   ")

    assert result.is_valid is False
    assert "cannot be empty" in (result.error_detail or "")


def test_verify_asset_simulation_flags():
    """Test asset verification with failure and invalid simulation flags."""
    connector_fail = VideoCDNConnector(simulate_failure=True)
    res_fail = connector_fail.verify_asset("v123")
    assert res_fail.is_valid is False
    assert "failure simulated" in (res_fail.error_detail or "")

    connector_inv = VideoCDNConnector(simulate_invalid=True)
    res_inv = connector_inv.verify_asset("v123")
    assert res_inv.is_valid is False
    assert "not found" in (res_inv.error_detail or "")


def test_get_embed_url_basic():
    """Test generating embed URL without tenant context or streaming token."""
    connector = VideoCDNConnector(cdn_url="https://media.test.com")
    result = connector.get_embed_url("vid_999")

    assert isinstance(result, VideoEmbedResultDTO)
    assert result.embed_url == "https://media.test.com/embed/vid_999"
    assert result.stream_url == "https://media.test.com/streams/vid_999.m3u8"
    assert result.video_id == "vid_999"
    assert result.expires_at is None


def test_get_embed_url_with_tenant_and_token():
    """Test generating embed URL with tenant identifier and streaming token."""
    connector = VideoCDNConnector(
        cdn_url="https://media.test.com",
        streaming_token="token_secret_123",
    )
    result = connector.get_embed_url("vid_999", tenant_id="tenant_abc")

    assert "tenant_id=tenant_abc" in result.embed_url
    assert "token=token_secret_123" in result.embed_url
    assert result.expires_at is not None
    assert isinstance(result.expires_at, datetime)
    assert result.metadata["tenant_context"] == "tenant_abc"
    assert result.metadata["has_streaming_token"] is True


def test_get_embed_url_validation_and_error():
    """Test embed URL generation failure handling for invalid inputs and simulated faults."""
    connector = VideoCDNConnector()
    with pytest.raises(VideoCDNValidationError):
        connector.get_embed_url("")

    connector_fail = VideoCDNConnector(simulate_failure=True)
    with pytest.raises(VideoCDNError):
        connector_fail.get_embed_url("v123")


def test_get_video_metadata_success():
    """Test retrieving video metadata."""
    connector = VideoCDNConnector(cdn_url="https://cdn.test.com")
    metadata = connector.get_video_metadata("module_video_101")

    assert isinstance(metadata, VideoMetadataDTO)
    assert metadata.video_id == "module_video_101"
    assert metadata.duration_seconds == 600
    assert metadata.thumbnail_url == "https://cdn.test.com/thumbnails/module_video_101.jpg"
    assert metadata.captions_available is True


def test_get_video_metadata_errors():
    """Test metadata retrieval exception cases."""
    connector = VideoCDNConnector()
    with pytest.raises(VideoCDNValidationError):
        connector.get_video_metadata("  ")

    connector_fail = VideoCDNConnector(simulate_failure=True)
    with pytest.raises(VideoCDNError):
        connector_fail.get_video_metadata("v123")


def test_external_alias():
    """Verify ExternalVideoCDNConnector alias works identically."""
    connector = ExternalVideoCDNConnector()
    assert isinstance(connector, VideoCDNConnector)
