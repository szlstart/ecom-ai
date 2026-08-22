from functools import lru_cache

from pydantic import Field
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
    redis_url: str = "redis://:local-redis-change-me@127.0.0.1:16379/0"

    object_storage_enabled: bool = False
    object_storage_endpoint: str = "http://127.0.0.1:19000"
    object_storage_access_key: str = ""
    object_storage_secret_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
