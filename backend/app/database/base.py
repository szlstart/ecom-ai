from datetime import datetime

from sqlalchemy import BigInteger, DateTime, MetaData, func
from sqlalchemy.dialects.mysql import BIGINT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

MYSQL_NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class MySQLBase(DeclarativeBase):
    metadata = MetaData(naming_convention=MYSQL_NAMING_CONVENTION)


class MutableMySQLModel:
    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.utc_timestamp(6), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        server_default=func.utc_timestamp(6),
        onupdate=func.utc_timestamp(6),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), nullable=False, default=0, server_default="0"
    )


class SoftDeleteMySQLModel(MutableMySQLModel):
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)


class AppendOnlyMySQLModel:
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(BIGINT(unsigned=True), "mysql"),
        primary_key=True,
        autoincrement=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.utc_timestamp(6), nullable=False
    )


class PostgreSQLBase(DeclarativeBase):
    pass
