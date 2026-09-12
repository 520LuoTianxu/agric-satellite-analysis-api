"""Add rgb preview columns on agri.parcel_scene_products.

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE agri.parcel_scene_products
          ADD COLUMN IF NOT EXISTS rgb_url text,
          ADD COLUMN IF NOT EXISTS large_rgb_url text,
          ADD COLUMN IF NOT EXISTS rgb_oss_key text
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN agri.parcel_scene_products.rgb_url IS
          'Parcel true-color preview URL (OSS signed or public)'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN agri.parcel_scene_products.large_rgb_url IS
          'Optional larger true-color preview URL'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN agri.parcel_scene_products.rgb_oss_key IS
          'Stable OSS object key for parcel true-color PNG'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE agri.parcel_scene_products
          DROP COLUMN IF EXISTS rgb_url,
          DROP COLUMN IF EXISTS large_rgb_url,
          DROP COLUMN IF EXISTS rgb_oss_key
        """
    )
