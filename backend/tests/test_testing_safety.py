import pytest

from app.core.config import Settings
from app.core.testing_safety import validate_integration_test_environment
from app.integrations.object_storage import MinioObjectStorage


def isolated_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "testing",
        "mysql_dsn": "mysql+asyncmy://user:pass@127.0.0.1/ecom_ai_test",
        "postgres_dsn": "postgresql+asyncpg://user:pass@127.0.0.1/ecom_ai_ai_test",
        "redis_url": "redis://127.0.0.1:6379/15",
        "object_storage_enabled": True,
        "object_storage_bucket_prefix": "test-run-",
    }
    values.update(overrides)
    return Settings.model_validate(values)


def test_isolated_integration_environment_is_accepted() -> None:
    validate_integration_test_environment(
        isolated_settings(), file_integration_enabled=True
    )


def test_object_storage_prefix_maps_logical_buckets_to_test_namespace() -> None:
    storage = MinioObjectStorage(isolated_settings())

    assert storage._physical_bucket("public-assets") == "test-run-public-assets"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"environment": "development"}, "ECOM_ENVIRONMENT"),
        ({"mysql_dsn": "mysql+asyncmy://user:pass@localhost/ecom_ai"}, "MySQL"),
        (
            {"postgres_dsn": "postgresql+asyncpg://user:pass@localhost/ecom_ai_ai"},
            "PostgreSQL",
        ),
        ({"redis_url": "redis://localhost:6379/0"}, "Redis"),
        ({"object_storage_bucket_prefix": ""}, "bucket prefix"),
    ],
)
def test_unsafe_integration_environment_is_rejected(
    override: dict[str, object], message: str
) -> None:
    with pytest.raises(RuntimeError, match=message):
        validate_integration_test_environment(
            isolated_settings(**override), file_integration_enabled=True
        )
