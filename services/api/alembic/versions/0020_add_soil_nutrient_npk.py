"""Add soil_nutrient_npk for vendor NPK (cdfinance analyzeSoilV2)

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "soil_nutrient_npk",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("uuid_generate_v4()"),
            nullable=False,
        ),
        sa.Column("field_id", sa.UUID(), nullable=False),
        sa.Column("land_id", sa.String(length=64), nullable=True),
        sa.Column(
            "source",
            sa.String(length=40),
            nullable=False,
            server_default="cdfinance_analyzeSoilV2",
        ),
        # Normalized primary nutrients (vendor indicator codes)
        sa.Column("tn_g_kg", sa.Float(), nullable=True),  # 全氮 TN
        sa.Column("an_mg_kg", sa.Float(), nullable=True),  # 碱解氮 AN
        sa.Column("ap_mg_kg", sa.Float(), nullable=True),  # 有效磷 AP
        sa.Column("ak_mg_kg", sa.Float(), nullable=True),  # 速效钾 AK
        sa.Column("tp_g_kg", sa.Float(), nullable=True),  # 全磷 TP
        sa.Column("tk_g_kg", sa.Float(), nullable=True),  # 全钾 TK
        sa.Column("som_g_kg", sa.Float(), nullable=True),  # 有机质 SOM
        sa.Column("ph", sa.Float(), nullable=True),
        sa.Column("sqi_score", sa.Float(), nullable=True),
        sa.Column("sqi_rating", sa.Text(), nullable=True),
        sa.Column("texture_usda_cn", sa.String(length=40), nullable=True),
        sa.Column("vendor_log_id", sa.BigInteger(), nullable=True),
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
        sa.UniqueConstraint("field_id", name="uq_soil_nutrient_npk_field"),
    )
    op.create_index("idx_soil_nutrient_npk_field_id", "soil_nutrient_npk", ["field_id"])
    op.create_index("idx_soil_nutrient_npk_land_id", "soil_nutrient_npk", ["land_id"])


def downgrade() -> None:
    op.drop_index("idx_soil_nutrient_npk_land_id", table_name="soil_nutrient_npk")
    op.drop_index("idx_soil_nutrient_npk_field_id", table_name="soil_nutrient_npk")
    op.drop_table("soil_nutrient_npk")
