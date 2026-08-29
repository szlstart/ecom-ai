import pytest
from pydantic import ValidationError

from app.core.config import Settings


def production_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "production",
        "build_sha": "c" * 40,
        "debug": False,
        "public_origin": "https://shop.acme.cn",
        "allowed_origins": "https://shop.acme.cn",
        "mysql_dsn": (
            "mysql+asyncmy://app:secret@mysql.private.acme.cn:3306/ecom_ai"
            "?charset=utf8mb4&ssl=true"
        ),
        "postgres_dsn": (
            "postgresql+asyncpg://app:secret@postgres.private.acme.cn:5432/ecom_ai_ai"
            "?ssl=require"
        ),
        "redis_url": "rediss://:secret@redis.private.acme.cn:6379/0",
        "access_token_secret": "a" * 32,
        "security_hmac_secret": "b" * 32,
        "debug_verification_code": None,
        "refresh_cookie_secure": True,
        "object_storage_enabled": True,
        "object_storage_endpoint": "https://objects.private.acme.cn",
        "object_storage_public_endpoint": "https://cdn.acme.cn",
        "object_storage_access_key": "access-key",
        "object_storage_secret_key": "secret-key",
        "otel_enabled": True,
        "otel_exporter_otlp_endpoint": "https://otel.private.acme.cn:4317",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_production_settings_accept_external_tls_dependencies() -> None:
    settings = production_settings()
    assert settings.environment == "production"


def test_production_settings_reject_unversioned_build() -> None:
    with pytest.raises(ValidationError):
        production_settings(build_sha="development")


def test_agent_worker_requires_complete_https_model_configuration() -> None:
    with pytest.raises(ValidationError):
        production_settings(agent_model_required=True)
    with pytest.raises(ValidationError):
        production_settings(
            agent_model_required=True,
            agent_model_api_url="http://models.private.acme.cn/v1/chat/completions",
            agent_model_api_key="secret",
            agent_model_name="approved-model",
        )
    settings = production_settings(
        agent_model_required=True,
        agent_model_api_url="https://models.private.acme.cn/v1/chat/completions",
        agent_model_api_key="secret",
        agent_model_name="approved-model",
    )
    assert settings.agent_model_name == "approved-model"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("public_origin", "http://shop.acme.cn"),
        ("mysql_dsn", "mysql+asyncmy://app:secret@localhost:3306/ecom_ai?ssl=true"),
        ("postgres_dsn", "postgresql+asyncpg://app:secret@postgres.acme.cn:5432/ecom_ai"),
        ("redis_url", "redis://:secret@redis.acme.cn:6379/0"),
        ("otel_enabled", False),
    ],
)
def test_production_settings_reject_insecure_dependencies(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        production_settings(**{field: value})
