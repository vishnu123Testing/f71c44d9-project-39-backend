import os
from unittest.mock import patch

from src.lib.config import Settings, get_settings


def test_default_settings() -> None:
    """Test that default configuration parameters are correctly set."""
    settings = Settings()
    assert settings.APP_NAME == "Platform Access Control System"
    assert settings.ENVIRONMENT == "development"
    assert settings.DEBUG is False
    assert settings.API_V1_STR == "/api/v1"
    assert settings.ALGORITHM == "HS256"
    assert settings.ACCESS_TOKEN_EXPIRE_MINUTES == 60
    assert settings.DATABASE_URL == "sqlite:///./app.db"
    assert settings.REDIS_URL == "redis://localhost:6379/0"
    assert settings.CONNECTOR_TIMEOUT == 5.0
    assert settings.CONNECTOR_RETRIES == 3
    assert settings.DEFAULT_POLLING_INTERVAL == 30
    assert settings.LOG_LEVEL == "INFO"
    assert settings.CORS_ORIGINS == ["http://localhost:3000", "http://127.0.0.1:3000"]
    assert settings.DEFAULT_SLA_THRESHOLD_HOURS == 24
    assert settings.SLA_P1_RESPONSE_HOURS == 1
    assert settings.SLA_P1_RESOLUTION_HOURS == 4
    assert settings.SLA_P2_RESPONSE_HOURS == 4
    assert settings.SLA_P2_RESOLUTION_HOURS == 24
    assert settings.SLA_P3_RESPONSE_HOURS == 24
    assert settings.SLA_P3_RESOLUTION_HOURS == 72
    assert settings.SLA_WARNING_THRESHOLD_PERCENT == 80.0
    assert settings.SLA_ESCALATION_THRESHOLD_PERCENT == 80.0
    assert settings.ESCALATION_RECIPIENT_ROLES == ["SalesManager", "Admin"]
    assert settings.SLA_SCAN_INTERVAL_MINUTES == 15
    assert settings.SLA_WORKER_SCHEDULE_SECONDS == 60
    assert settings.MAX_ATTACHMENT_SIZE_BYTES == 10 * 1024 * 1024
    assert settings.ALLOWED_ATTACHMENT_EXTENSIONS == [
        ".pdf",
        ".docx",
        ".doc",
        ".txt",
        ".png",
        ".jpg",
        ".jpeg",
    ]
    assert settings.ALLOWED_ATTACHMENT_MIME_TYPES == [
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/plain",
        "image/png",
        "image/jpeg",
    ]
    assert settings.SUPPORT_ATTACHMENTS_DIR == "./storage/attachments"
    assert settings.STORAGE_BUCKET_NAME is None
    assert settings.STORAGE_ACCESS_KEY is None
    assert settings.STORAGE_SECRET_KEY is None
    assert settings.CSAT_MIN_SCORE == 1
    assert settings.CSAT_MAX_SCORE == 5
    assert settings.FORECAST_PROVIDER == "default"
    assert settings.FORECAST_PROVIDER_URL is None
    assert settings.FORECAST_PROVIDER_API_KEY is None
    assert settings.FORECAST_MIN_HISTORICAL_TRANSITIONS == 3
    assert settings.FORECAST_DEFAULT_CONFIDENCE_PCT == 75.0
    assert settings.FORECAST_MIN_CONFIDENCE_THRESHOLD == 0.0
    assert settings.ESCALATION_WORKER_SCHEDULE_SECONDS == 900
    assert settings.FORECAST_RECALCULATION_SCHEDULE_SECONDS == 300
    assert settings.EXPORT_OUTPUT_DIR == "./exports"
    assert settings.ANALYTICS_EXPORT_DIR == "./exports"
    assert settings.ALLOWED_EXPORT_FORMATS == ["csv", "pdf"]
    assert settings.DEFAULT_BOTTLENECK_DROPOFF_THRESHOLD == 50.0
    assert settings.DEFAULT_FUNNEL_BOTTLENECK_THRESHOLD == 50.0
    assert settings.ANALYTICS_SECRET_KEY is None
    assert settings.MARKETING_INTEGRATION_KEY is None


def test_custom_environment_settings() -> None:
    """Test environment variable overrides for configuration settings."""
    env_vars = {
        "APP_NAME": "Custom App Name",
        "ENVIRONMENT": "production",
        "DEBUG": "true",
        "DATABASE_URL": "postgresql://user:pass@localhost:5432/dbname",
        "LOG_LEVEL": "DEBUG",
        "SECRET_KEY": "super-secret-key-for-testing-override-purposes",
        "CONNECTOR_TIMEOUT": "10.0",
        "CONNECTOR_RETRIES": "5",
        "DEFAULT_POLLING_INTERVAL": "60",
        "DEFAULT_SLA_THRESHOLD_HOURS": "48",
        "SLA_P1_RESPONSE_HOURS": "2",
        "SLA_P1_RESOLUTION_HOURS": "8",
        "SLA_P2_RESPONSE_HOURS": "6",
        "SLA_P2_RESOLUTION_HOURS": "36",
        "SLA_P3_RESPONSE_HOURS": "48",
        "SLA_P3_RESOLUTION_HOURS": "144",
        "SLA_WARNING_THRESHOLD_PERCENT": "75.0",
        "SLA_ESCALATION_THRESHOLD_PERCENT": "85.0",
        "SLA_SCAN_INTERVAL_MINUTES": "30",
        "MAX_ATTACHMENT_SIZE_BYTES": "20971520",
        "SUPPORT_ATTACHMENTS_DIR": "/tmp/custom_attachments",
        "STORAGE_BUCKET_NAME": "my-bucket",
        "STORAGE_ACCESS_KEY": "access-key",
        "STORAGE_SECRET_KEY": "secret-key",
        "FORECAST_PROVIDER": "custom_ml",
        "FORECAST_PROVIDER_URL": "https://ai.example.com",
        "FORECAST_PROVIDER_API_KEY": "secret-api-key",
        "FORECAST_MIN_HISTORICAL_TRANSITIONS": "5",
        "FORECAST_DEFAULT_CONFIDENCE_PCT": "80.0",
        "FORECAST_MIN_CONFIDENCE_THRESHOLD": "10.0",
        "ESCALATION_WORKER_SCHEDULE_SECONDS": "1800",
        "FORECAST_RECALCULATION_SCHEDULE_SECONDS": "600",
        "EXPORT_OUTPUT_DIR": "/tmp/custom_exports",
        "ANALYTICS_EXPORT_DIR": "/tmp/custom_exports",
        "DEFAULT_BOTTLENECK_DROPOFF_THRESHOLD": "40.0",
        "DEFAULT_FUNNEL_BOTTLENECK_THRESHOLD": "40.0",
        "ANALYTICS_SECRET_KEY": "analytics-secret",
        "MARKETING_INTEGRATION_KEY": "mktg-key",
    }
    with patch.dict(os.environ, env_vars):
        settings = Settings()
        assert settings.APP_NAME == "Custom App Name"
        assert settings.ENVIRONMENT == "production"
        assert settings.DEBUG is True
        assert settings.DATABASE_URL == "postgresql://user:pass@localhost:5432/dbname"
        assert settings.LOG_LEVEL == "DEBUG"
        assert settings.SECRET_KEY == "super-secret-key-for-testing-override-purposes"
        assert settings.CONNECTOR_TIMEOUT == 10.0
        assert settings.CONNECTOR_RETRIES == 5
        assert settings.DEFAULT_POLLING_INTERVAL == 60
        assert settings.DEFAULT_SLA_THRESHOLD_HOURS == 48
        assert settings.SLA_P1_RESPONSE_HOURS == 2
        assert settings.SLA_P1_RESOLUTION_HOURS == 8
        assert settings.SLA_P2_RESPONSE_HOURS == 6
        assert settings.SLA_P2_RESOLUTION_HOURS == 36
        assert settings.SLA_P3_RESPONSE_HOURS == 48
        assert settings.SLA_P3_RESOLUTION_HOURS == 144
        assert settings.SLA_WARNING_THRESHOLD_PERCENT == 75.0
        assert settings.SLA_ESCALATION_THRESHOLD_PERCENT == 85.0
        assert settings.SLA_SCAN_INTERVAL_MINUTES == 30
        assert settings.MAX_ATTACHMENT_SIZE_BYTES == 20971520
        assert settings.SUPPORT_ATTACHMENTS_DIR == "/tmp/custom_attachments"
        assert settings.STORAGE_BUCKET_NAME == "my-bucket"
        assert settings.STORAGE_ACCESS_KEY == "access-key"
        assert settings.STORAGE_SECRET_KEY == "secret-key"
        assert settings.FORECAST_PROVIDER == "custom_ml"
        assert settings.FORECAST_PROVIDER_URL == "https://ai.example.com"
        assert settings.FORECAST_PROVIDER_API_KEY == "secret-api-key"
        assert settings.FORECAST_MIN_HISTORICAL_TRANSITIONS == 5
        assert settings.FORECAST_DEFAULT_CONFIDENCE_PCT == 80.0
        assert settings.FORECAST_MIN_CONFIDENCE_THRESHOLD == 10.0
        assert settings.ESCALATION_WORKER_SCHEDULE_SECONDS == 1800
        assert settings.FORECAST_RECALCULATION_SCHEDULE_SECONDS == 600
        assert settings.EXPORT_OUTPUT_DIR == "/tmp/custom_exports"
        assert settings.ANALYTICS_EXPORT_DIR == "/tmp/custom_exports"
        assert settings.DEFAULT_BOTTLENECK_DROPOFF_THRESHOLD == 40.0
        assert settings.DEFAULT_FUNNEL_BOTTLENECK_THRESHOLD == 40.0
        assert settings.ANALYTICS_SECRET_KEY == "analytics-secret"
        assert settings.MARKETING_INTEGRATION_KEY == "mktg-key"


def test_cors_origins_parsing() -> None:
    """Test CORS_ORIGINS parsing from comma-separated string."""
    env_vars = {
        "CORS_ORIGINS": "http://example.com, https://app.example.com",
    }
    with patch.dict(os.environ, env_vars):
        settings = Settings()
        assert settings.CORS_ORIGINS == [
            "http://example.com",
            "https://app.example.com",
        ]


def test_escalation_recipient_roles_parsing() -> None:
    """Test ESCALATION_RECIPIENT_ROLES parsing from comma-separated string."""
    env_vars = {
        "ESCALATION_RECIPIENT_ROLES": "Director, VP_Sales",
    }
    with patch.dict(os.environ, env_vars):
        settings = Settings()
        assert settings.ESCALATION_RECIPIENT_ROLES == ["Director", "VP_Sales"]


def test_allowed_export_formats_parsing() -> None:
    """Test ALLOWED_EXPORT_FORMATS parsing from comma-separated string."""
    env_vars = {
        "ALLOWED_EXPORT_FORMATS": "csv, pdf, json",
    }
    with patch.dict(os.environ, env_vars):
        settings = Settings()
        assert settings.ALLOWED_EXPORT_FORMATS == ["csv", "pdf", "json"]


def test_allowed_attachment_parsing() -> None:
    """Test ALLOWED_ATTACHMENT_EXTENSIONS and ALLOWED_ATTACHMENT_MIME_TYPES parsing."""
    env_vars = {
        "ALLOWED_ATTACHMENT_EXTENSIONS": ".pdf, .txt, .png",
        "ALLOWED_ATTACHMENT_MIME_TYPES": "application/pdf, text/plain, image/png",
    }
    with patch.dict(os.environ, env_vars):
        settings = Settings()
        assert settings.ALLOWED_ATTACHMENT_EXTENSIONS == [".pdf", ".txt", ".png"]
        assert settings.ALLOWED_ATTACHMENT_MIME_TYPES == [
            "application/pdf",
            "text/plain",
            "image/png",
        ]


def test_get_settings_cached() -> None:
    """Test get_settings() caching mechanism."""
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
