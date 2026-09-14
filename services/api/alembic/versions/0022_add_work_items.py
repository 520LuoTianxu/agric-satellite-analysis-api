"""Add work_items table for HTTP claim control plane (download-host isolation).

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "work_items",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("uuid_generate_v4()"),
            nullable=False,
        ),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "priority",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column(
            "result_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "progress_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_work_items_idempotency_key"),
    )
    op.create_index(
        "idx_work_items_status_priority_created",
        "work_items",
        ["status", "priority", "created_at"],
    )
    op.create_index(
        "idx_work_items_pending",
        "work_items",
        ["priority", "created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "idx_work_items_leased_until",
        "work_items",
        ["lease_until"],
        postgresql_where=sa.text("status = 'leased'"),
    )
    op.create_index("idx_work_items_type", "work_items", ["type"])


def downgrade() -> None:
    op.drop_index("idx_work_items_type", table_name="work_items")
    op.drop_index("idx_work_items_leased_until", table_name="work_items")
    op.drop_index("idx_work_items_pending", table_name="work_items")
    op.drop_index("idx_work_items_status_priority_created", table_name="work_items")
    op.drop_table("work_items")
