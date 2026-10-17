from functools import lru_cache
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings and environment configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # General Application Configuration
    APP_NAME: str = "Platform Access Control System"
    ENVIRONMENT: str = "development"
    DEBUG: bool = False
    API_V1_STR: str = "/api/v1"

    # Security & Authentication Configuration
    SECRET_KEY: str = (
        "09d25e094faa6ca2556c818166b7a9563b93f7099f6f0f4caa6cf63b88e8d3e7"
    )
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # Persistence Configuration
    DATABASE_URL: str = "sqlite:///./app.db"

    # Queue & Worker Configuration
    REDIS_URL: str = "redis://localhost:6379/0"

    # Dashboard & Connector Configuration
    CONNECTOR_TIMEOUT: float = 5.0
    CONNECTOR_RETRIES: int = 3
    DEFAULT_POLLING_INTERVAL: int = 30

    # Storage & File Upload Configuration
    MAX_UPLOAD_SIZE_BYTES: int = 10 * 1024 * 1024  # 10 MB limit
    MAX_ATTACHMENT_SIZE_BYTES: int = 10 * 1024 * 1024  # 10 MB limit
    ALLOWED_UPLOAD_EXTENSIONS: list[str] = [".pdf", ".docx", ".doc", ".txt"]
    ALLOWED_ATTACHMENT_EXTENSIONS: list[str] = [
        ".pdf",
        ".docx",
        ".doc",
        ".txt",
        ".png",
        ".jpg",
        ".jpeg",
    ]
    ALLOWED_ATTACHMENT_MIME_TYPES: list[str] = [
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/plain",
        "image/png",
        "image/jpeg",
    ]
    STORAGE_ROOT: str = "./storage"
    SUPPORT_ATTACHMENTS_DIR: str = "./storage/attachments"
    STORAGE_PROVIDER: str = "local"
    STORAGE_BUCKET_NAME: str | None = None
    STORAGE_ACCESS_KEY: str | None = None
    STORAGE_SECRET_KEY: str | None = None
    STORAGE_ENCRYPTION_KEY: str | None = None

    # Contract Alert & Notification Configuration
    RENEWAL_ALERT_THRESHOLDS: list[int] = [90, 60, 30]
    ALERT_NOTIFICATION_CHANNEL: str = "in_app"
    NOTIFICATION_DISPATCH_ENABLED: bool = True

    # Multi-Channel Alerting & Notification Credentials & Gateway Settings
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    SMS_GATEWAY_URL: str | None = None
    SMS_API_KEY: str | None = None
    ALERT_SUPPRESSION_WINDOW_MINUTES: int = 15
    ALERT_SUPPRESSION_WINDOW_SECONDS: int = 900

    # SLA & Escalation Configuration
    DEFAULT_SLA_THRESHOLD_HOURS: int = 24
    SLA_P1_RESPONSE_HOURS: int = 1
    SLA_P1_RESOLUTION_HOURS: int = 4
    SLA_P2_RESPONSE_HOURS: int = 4
    SLA_P2_RESOLUTION_HOURS: int = 24
    SLA_P3_RESPONSE_HOURS: int = 24
    SLA_P3_RESOLUTION_HOURS: int = 72
    SLA_WARNING_THRESHOLD_PERCENT: float = 80.0
    SLA_ESCALATION_THRESHOLD_PERCENT: float = 80.0
    ESCALATION_RECIPIENT_ROLES: list[str] = ["SalesManager", "Admin"]
    SLA_SCAN_INTERVAL_MINUTES: int = 15
    SLA_WORKER_SCHEDULE_SECONDS: int = 60

    # CSAT Configuration
    CSAT_MIN_SCORE: int = 1
    CSAT_MAX_SCORE: int = 5

    # AI Forecasting Parameters
    FORECAST_PROVIDER: str = "default"
    FORECAST_PROVIDER_URL: str | None = None
    FORECAST_PROVIDER_API_KEY: str | None = None
    FORECAST_MIN_HISTORICAL_TRANSITIONS: int = 3
    FORECAST_DEFAULT_CONFIDENCE_PCT: float = 75.0
    FORECAST_MIN_CONFIDENCE_THRESHOLD: float = 0.0

    # Deal Pipeline Worker Schedule Settings
    ESCALATION_WORKER_SCHEDULE_SECONDS: int = 900  # 15 minutes
    FORECAST_RECALCULATION_SCHEDULE_SECONDS: int = 300  # 5 minutes

    # Analytics & Export Configuration
    EXPORT_OUTPUT_DIR: str = "./exports"
    ANALYTICS_EXPORT_DIR: str = "./exports"
    ALLOWED_EXPORT_FORMATS: list[str] = ["csv", "pdf"]
    DEFAULT_BOTTLENECK_DROPOFF_THRESHOLD: float = 50.0
    DEFAULT_FUNNEL_BOTTLENECK_THRESHOLD: float = 50.0
    ANALYTICS_SECRET_KEY: str | None = None
    MARKETING_INTEGRATION_KEY: str | None = None

    # Knowledge Repository & CDN Provider Configuration
    KNOWLEDGE_STORAGE_DIR: str = "./storage/knowledge"
    KNOWLEDGE_CDN_PROVIDER: str = "default"
    KNOWLEDGE_CDN_URL: str | None = None
    KNOWLEDGE_CDN_API_KEY: str | None = None
    KNOWLEDGE_CDN_STREAMING_TOKEN: str | None = None
    KNOWLEDGE_DEFAULT_PAGE_SIZE: int = 20
    KNOWLEDGE_MAX_PAGE_SIZE: int = 100

    # Observability Configuration
    LOG_LEVEL: str = "INFO"

    # Cross-Origin Resource Sharing
    CORS_ORIGINS: list[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v: Any) -> list[str] | str:
        """Parse CORS origins from a comma-separated string or list."""
        if isinstance(v, str) and not v.startswith("["):
            return [i.strip() for i in v.split(",") if i.strip()]
        if isinstance(v, list | tuple):
            return [str(i) for i in v]
        return v

    @field_validator("ALLOWED_UPLOAD_EXTENSIONS", mode="before")
    @classmethod
    def parse_allowed_extensions(cls, v: Any) -> list[str]:
        """Parse allowed file extensions from a comma-separated string or list."""
        if isinstance(v, str) and not v.startswith("["):
            return [i.strip() for i in v.split(",") if i.strip()]
        if isinstance(v, list | tuple):
            return [str(i) for i in v]
        return v

    @field_validator("ALLOWED_ATTACHMENT_EXTENSIONS", mode="before")
    @classmethod
    def parse_allowed_attachment_extensions(cls, v: Any) -> list[str]:
        """Parse allowed attachment extensions from a comma-separated string or list."""
        if isinstance(v, str) and not v.startswith("["):
            return [i.strip() for i in v.split(",") if i.strip()]
        if isinstance(v, list | tuple):
            return [str(i) for i in v]
        return v

    @field_validator("ALLOWED_ATTACHMENT_MIME_TYPES", mode="before")
    @classmethod
    def parse_allowed_attachment_mime_types(cls, v: Any) -> list[str]:
        """Parse allowed attachment MIME types from a comma-separated string or list."""
        if isinstance(v, str) and not v.startswith("["):
            return [i.strip() for i in v.split(",") if i.strip()]
        if isinstance(v, list | tuple):
            return [str(i) for i in v]
        return v

    @field_validator("RENEWAL_ALERT_THRESHOLDS", mode="before")
    @classmethod
    def parse_alert_thresholds(cls, v: Any) -> list[int]:
        """Parse renewal alert thresholds from a comma-separated string or list."""
        if isinstance(v, str) and not v.startswith("["):
            return [int(i.strip()) for i in v.split(",") if i.strip()]
        if isinstance(v, list | tuple):
            return [int(i) for i in v]
        return v

    @field_validator("ESCALATION_RECIPIENT_ROLES", mode="before")
    @classmethod
    def parse_escalation_recipient_roles(cls, v: Any) -> list[str]:
        """Parse escalation recipient roles from a comma-separated string or list."""
        if isinstance(v, str) and not v.startswith("["):
            return [i.strip() for i in v.split(",") if i.strip()]
        if isinstance(v, list | tuple):
            return [str(i) for i in v]
        return v

    @field_validator("ALLOWED_EXPORT_FORMATS", mode="before")
    @classmethod
    def parse_allowed_export_formats(cls, v: Any) -> list[str]:
        """Parse allowed export formats from a comma-separated string or list."""
        if isinstance(v, str) and not v.startswith("["):
            return [i.strip() for i in v.split(",") if i.strip()]
        if isinstance(v, list | tuple):
            return [str(i) for i in v]
        return v


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings instance."""
    return Settings()


settings: Settings = get_settings()
