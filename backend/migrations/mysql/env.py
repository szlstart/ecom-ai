from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import get_settings
from app.database.base import MySQLBase
from app.modules.after_sale import models as after_sale_models  # noqa: F401
from app.modules.cart import models as cart_models  # noqa: F401
from app.modules.catalog import models as catalog_models  # noqa: F401
from app.modules.checkout import models as checkout_models  # noqa: F401
from app.modules.content import models as content_models  # noqa: F401
from app.modules.files import models as file_models  # noqa: F401
from app.modules.identity import models as identity_models  # noqa: F401
from app.modules.inventory import models as inventory_models  # noqa: F401
from app.modules.logistics import models as logistics_models  # noqa: F401
from app.modules.messaging import models as messaging_models  # noqa: F401
from app.modules.orders import models as order_models  # noqa: F401
from app.modules.payments import models as payment_models  # noqa: F401
from app.modules.rbac import models as rbac_models  # noqa: F401
from app.modules.reviews import models as review_models  # noqa: F401
from app.modules.stores import models as store_models  # noqa: F401
from app.modules.system import models as system_models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
config.set_main_option("sqlalchemy.url", get_settings().mysql_dsn.replace("+asyncmy", "+pymysql"))
target_metadata = MySQLBase.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_offline() if context.is_offline_mode() else run_migrations_online()
