from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_prefix="ECOM_",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "ecom-ai API"
    app_version: str = "0.1.0"
    environment: str = "development"
    debug: bool = False
    log_level: str = "INFO"
    otel_enabled: bool = False
    otel_service_name: str = "ecom-ai-api"
    otel_exporter_otlp_endpoint: str = "http://127.0.0.1:4317"
    api_v1_prefix: str = "/api/v1"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    readiness_checks_enabled: bool = True
    dependency_timeout_seconds: float = Field(default=2.0, gt=0, le=10)

    mysql_dsn: str = (
        "mysql+asyncmy://ecom_app:local-app-change-me@127.0.0.1:13306/ecom_ai?charset=utf8mb4"
    )
    postgres_dsn: str = (
        "postgresql+asyncpg://ecom_ai:local-postgres-change-me@127.0.0.1:15432/ecom_ai_ai"
    )
    embedding_api_url: str | None = None
    embedding_api_key: SecretStr | None = None
    embedding_model: str = "ecom-multilingual-v1"
    embedding_dimension: int = Field(default=1536, ge=1, le=4096)
    embedding_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    redis_url: str = "redis://:local-redis-change-me@127.0.0.1:16379/0"
    realtime_ticket_ttl_seconds: int = Field(default=30, ge=10, le=120)
    realtime_connection_lease_seconds: int = Field(default=75, ge=30, le=180)
    realtime_heartbeat_seconds: int = Field(default=25, ge=10, le=60)
    realtime_connection_queue_size: int = Field(default=100, ge=10, le=1000)
    realtime_max_client_frame_bytes: int = Field(default=4096, ge=256, le=65536)
    realtime_outbox_poll_seconds: float = Field(default=0.5, ge=0.1, le=10)
    realtime_outbox_batch_size: int = Field(default=100, ge=1, le=1000)

    access_token_secret: SecretStr = SecretStr("development-access-token-secret-change-me")
    security_hmac_secret: SecretStr = SecretStr("development-hmac-secret-change-me")
    field_encryption_key: SecretStr = SecretStr("MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
    access_token_ttl_seconds: int = Field(default=900, ge=300, le=3600)
    refresh_token_ttl_days: int = Field(default=30, ge=1, le=90)
    admin_refresh_token_ttl_hours: int = Field(default=8, ge=1, le=24)
    admin_recent_auth_seconds: int = Field(default=300, ge=60, le=1800)
    password_min_length: int = Field(default=15, ge=15, le=64)
    password_max_length: int = Field(default=128, ge=64, le=256)
    refresh_cookie_secure: bool = False
    allowed_origins: str = "http://127.0.0.1:5173,http://127.0.0.1:8080"
    debug_verification_code: SecretStr | None = None

    object_storage_enabled: bool = False
    object_storage_endpoint: str = "http://127.0.0.1:19000"
    object_storage_public_endpoint: str = "http://127.0.0.1:19000"
    object_storage_access_key: str = ""
    object_storage_secret_key: str = ""
    object_storage_region: str = "us-east-1"
    object_storage_presign_seconds: int = Field(default=600, ge=60, le=3600)
    file_scanner_enabled: bool = False
    file_scanner_host: str = "127.0.0.1"
    file_scanner_port: int = Field(default=13310, ge=1, le=65535)
    file_scanner_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    order_timeout_poll_seconds: float = Field(default=2.0, ge=0.5, le=60)
    order_timeout_batch_size: int = Field(default=100, ge=1, le=1000)
    payment_reconcile_poll_seconds: float = Field(default=2.0, ge=0.5, le=60)
    payment_reconcile_batch_size: int = Field(default=100, ge=1, le=1000)
    logistics_sync_poll_seconds: float = Field(default=2.0, ge=0.5, le=60)
    logistics_sync_stale_seconds: int = Field(default=300, ge=30, le=86_400)
    logistics_sync_batch_size: int = Field(default=100, ge=1, le=1000)
    review_submission_window_days: int = Field(default=30, ge=1, le=365)
    review_edit_window_hours: int = Field(default=24, ge=1, le=168)
    review_append_window_days: int = Field(default=180, ge=1, le=730)
    refund_dual_approval_threshold_minor: int = Field(default=50_000, ge=1)
    admin_approval_ttl_minutes: int = Field(default=30, ge=5, le=240)
    admin_approval_worker_poll_seconds: float = Field(default=2.0, ge=0.5, le=60)

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]

    @model_validator(mode="after")
    def reject_development_secrets_in_production(self) -> "Settings":
        if self.environment.lower() != "production":
            return self
        secrets = (
            self.access_token_secret.get_secret_value(),
            self.security_hmac_secret.get_secret_value(),
        )
        if any("development" in secret or "change-me" in secret for secret in secrets):
            raise ValueError("production authentication secrets must be provided")
        if self.debug_verification_code is not None:
            raise ValueError("debug verification code is forbidden in production")
        if not self.refresh_cookie_secure:
            raise ValueError("Secure refresh cookies are required in production")
        if self.embedding_api_url and self.embedding_api_key is None:
            raise ValueError("embedding API key is required when an embedding API URL is set")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
