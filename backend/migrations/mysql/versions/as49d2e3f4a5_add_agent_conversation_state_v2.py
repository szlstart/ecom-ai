"""add typed Agent conversation state v2

Revision ID: as49d2e3f4a5
Revises: ar38c1d2e3f4
"""

from alembic import op

revision = "as49d2e3f4a5"
down_revision = "ar38c1d2e3f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE `ai_conversation_states` (
          `conversation_id` BIGINT UNSIGNED NOT NULL,
          `state_schema_version` VARCHAR(32) NOT NULL DEFAULT 'conversation_state_v2',
          `topic_generation` BIGINT UNSIGNED NOT NULL DEFAULT 1,
          `source_sequence_no` BIGINT UNSIGNED NOT NULL DEFAULT 0,
          `state_payload` JSON NOT NULL,
          `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
          `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
          `updated_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
          `version` BIGINT UNSIGNED NOT NULL DEFAULT 0,
          PRIMARY KEY (`id`),
          CONSTRAINT `uk_ai_conversation_states_conversation` UNIQUE (`conversation_id`),
          KEY `idx_ai_conversation_states_source` (`conversation_id`, `source_sequence_no`),
          CONSTRAINT `fk_ai_conversation_states_conversation_id_conversations`
            FOREIGN KEY (`conversation_id`) REFERENCES `conversations` (`id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE `ai_conversation_states`")
