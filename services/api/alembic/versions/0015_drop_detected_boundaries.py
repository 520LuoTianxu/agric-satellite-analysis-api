"""Drop detected_boundaries table (ML boundary detection removed).

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-09
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers
revision: str = "0015"
down_revision: str | None = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS set_detected_boundaries_updated_at ON detected_boundaries"
    )
    op.drop_table("detected_boundaries")


def downgrade() -> None:
    op.create_table(
        "detected_boundaries",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column(
            "org_id",
            UUID(as_uuid=True),
            sa.ForeignKey("orgs.id"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("jobs.id"),
            nullable=False,
        ),
        sa.Column("area_ha", sa.Numeric(12, 4), nullable=True),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "accepted_field_id",
            UUID(as_uuid=True),
            sa.ForeignKey("fields.id"),
            nullable=True,
        ),
        sa.Column("detection_date", sa.Date, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "ALTER TABLE detected_boundaries "
        "ADD COLUMN geom geometry(MULTIPOLYGON, 4326) NOT NULL"
    )
    op.create_index("ix_detected_boundaries_org_id", "detected_boundaries", ["org_id"])
    op.create_index("ix_detected_boundaries_job_id", "detected_boundaries", ["job_id"])
    op.create_index("ix_detected_boundaries_status", "detected_boundaries", ["status"])
    op.create_index(
        "ix_detected_boundaries_geom",
        "detected_boundaries",
        ["geom"],
        postgresql_using="gist",
    )
    op.execute(
        """
        CREATE TRIGGER set_detected_boundaries_updated_at
        BEFORE UPDATE ON detected_boundaries
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )
