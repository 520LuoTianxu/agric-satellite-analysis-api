"""Add agri.mq_task_results for CloudAMQP result writer.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-10
"""

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS agri")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agri.mq_task_results (
            task_id text PRIMARY KEY,
            status text NOT NULL,
            oss_urls jsonb NOT NULL DEFAULT '{}'::jsonb,
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            error text,
            field_id text,
            land_id text,
            finished_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT mq_task_results_status_ck
                CHECK (status = ANY (ARRAY['success'::text, 'failed'::text]))
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_mq_task_results_updated_at
            ON agri.mq_task_results (updated_at DESC)
        """
    )
    op.execute(
        """
        COMMENT ON TABLE agri.mq_task_results IS
            'CloudAMQP outer bus results (OSS URLs + downloaded JSON payloads)'
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agri.mq_task_results")
