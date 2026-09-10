"""Drop org_id columns and users/orgs auth tables.

DESTRUCTIVE:
- Drops org_id columns (+ indexes) from all domain tables.
- Drops user-attribution columns (created_by / user_id / revoked_by) from
  domain tables so users/orgs tables can be removed.
- Drops auth tables: invites, org_members, orgs, users (FK order).

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels = None
depends_on = None

ORG_ID_COLUMNS = [
    "farms",
    "fields",
    "raster_layers",
    "field_stats",
    "alerts",
    "scouting_observations",
    "jobs",
    "audit_events",
    "share_links",
    "weather_daily",
    "soil_profiles",
]

ORG_ID_INDEXES = [
    ("weather_daily", "idx_weather_org_id"),
    ("soil_profiles", "idx_soil_profiles_org_id"),
]

# Domain columns that referenced users (FKs already dropped in 0017).
USER_ATTR_COLUMNS = [
    ("fields", "created_by"),
    ("scouting_observations", "created_by"),
    ("jobs", "created_by"),
    ("audit_events", "user_id"),
    ("share_links", "created_by"),
    ("share_links", "revoked_by"),
]


def _drop_fk_on_column(table: str, column: str) -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            """
            SELECT tc.constraint_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.table_schema = 'public'
              AND tc.table_name = :table
              AND tc.constraint_type = 'FOREIGN KEY'
              AND kcu.column_name = :column
            """
        ),
        {"table": table, "column": column},
    ).fetchall()
    for (name,) in rows:
        op.drop_constraint(name, table, type_="foreignkey")


def _table_exists(table: str) -> bool:
    conn = op.get_bind()
    row = conn.execute(
        sa.text(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = :t
            """
        ),
        {"t": table},
    ).fetchone()
    return row is not None


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    row = conn.execute(
        sa.text(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = :t AND column_name = :c
            """
        ),
        {"t": table, "c": column},
    ).fetchone()
    return row is not None


def _index_exists(name: str) -> bool:
    conn = op.get_bind()
    row = conn.execute(
        sa.text(
            """
            SELECT 1 FROM pg_indexes
            WHERE schemaname = 'public' AND indexname = :n
            """
        ),
        {"n": name},
    ).fetchone()
    return row is not None


def upgrade() -> None:
    for table, index_name in ORG_ID_INDEXES:
        if _index_exists(index_name):
            op.drop_index(index_name, table_name=table)

    for table in ORG_ID_COLUMNS:
        if _column_exists(table, "org_id"):
            # Any leftover FK (e.g. partial apply)
            _drop_fk_on_column(table, "org_id")
            op.drop_column(table, "org_id")

    for table, column in USER_ATTR_COLUMNS:
        if _column_exists(table, column):
            _drop_fk_on_column(table, column)
            op.drop_column(table, column)

    # Auth tables — drop children first.
    for table in ("invites", "org_members", "orgs", "users"):
        if _table_exists(table):
            op.drop_table(table)


def downgrade() -> None:
    # Best-effort recreate of empty auth tables + nullable org_id columns.
    # Data is NOT restored.
    op.create_table(
        "users",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
    )
    op.create_table(
        "orgs",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "org_members",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="member"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("org_id", "user_id", name="uq_org_member"),
    )
    op.create_table(
        "invites",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="member"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("invited_by", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
    )

    for table in ORG_ID_COLUMNS:
        op.add_column(
            table,
            sa.Column("org_id", UUID(as_uuid=True), nullable=True),
        )
    for table, column in USER_ATTR_COLUMNS:
        op.add_column(
            table,
            sa.Column(column, UUID(as_uuid=True), nullable=True),
        )
    op.create_index("idx_weather_org_id", "weather_daily", ["org_id"])
    op.create_index("idx_soil_profiles_org_id", "soil_profiles", ["org_id"])
