from datetime import datetime

from sqlalchemy import Boolean, Computed, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.mysql import BIGINT
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import MutableMySQLModel, MySQLBase, SoftDeleteMySQLModel


class Conversation(SoftDeleteMySQLModel, MySQLBase):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("conversation_no", name="uk_conversations_no"),
        UniqueConstraint("exclusive_user_key", name="uk_conversations_exclusive_user"),
        Index("idx_conversations_user_updated", "user_id", "updated_at", "id"),
    )

    conversation_no: Mapped[str] = mapped_column(String(40), nullable=False)
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    store_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    conversation_type: Mapped[str] = mapped_column(String(16), nullable=False)
    is_fixed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    exclusive_user_key: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True),
        Computed(
            "CASE WHEN conversation_type = 'exclusive' "
            "AND deleted_at IS NULL THEN user_id ELSE NULL END"
        ),
    )
    conversation_status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class ConversationStatusLog(MutableMySQLModel, MySQLBase):
    __tablename__ = "conversation_status_logs"

    conversation_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("conversations.id"), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
