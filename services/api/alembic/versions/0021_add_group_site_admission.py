"""Add group_site_admission for cdfinance site questionnaire

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "group_site_admission",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("uuid_generate_v4()"),
            nullable=False,
        ),
        sa.Column("field_id", sa.UUID(), nullable=True),
        sa.Column("group_id", sa.String(length=64), nullable=False),
        sa.Column("land_id", sa.String(length=64), nullable=True),
        sa.Column(
            "source",
            sa.String(length=40),
            nullable=False,
            server_default="cdfinance_groupSiteAdmission",
        ),
        # Normalized summary
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("score_bank", sa.String(length=80), nullable=True),
        sa.Column("survey_id", sa.BigInteger(), nullable=True),
        sa.Column("answer_id", sa.BigInteger(), nullable=True),
        sa.Column("total_area_mu", sa.Float(), nullable=True),
        sa.Column("avg_yield", sa.Float(), nullable=True),
        sa.Column("mu_profit", sa.Float(), nullable=True),
        sa.Column(
            "summary_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "vendor_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.ForeignKeyConstraint(["field_id"], ["fields.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", name="uq_group_site_admission_group"),
    )
    op.create_index(
        "idx_group_site_admission_field_id", "group_site_admission", ["field_id"]
    )
    op.create_index(
        "idx_group_site_admission_land_id", "group_site_admission", ["land_id"]
    )


def downgrade() -> None:
    op.drop_index("idx_group_site_admission_land_id", table_name="group_site_admission")
    op.drop_index(
        "idx_group_site_admission_field_id", table_name="group_site_admission"
    )
    op.drop_table("group_site_admission")
