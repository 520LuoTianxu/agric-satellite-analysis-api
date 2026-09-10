"""Drop org_id / user FKs for OpenFarm auth removal.

DESTRUCTIVE / schema-loosening:
- Drops all foreign keys from domain tables to orgs.id (org_id columns).
- Makes org_id nullable on domain tables so MQ/API writers can omit org.
- Drops selected FKs to users.id (created_by / user_id) on domain tables and
  makes those columns nullable so writes work without a users row.

Does NOT drop users/orgs/org_members/invites tables yet (follow-up).
Does NOT drop org_id columns yet (follow-up) — leftover columns documented
in the PR body.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels = None
depends_on = None

# (table, column) pairs that currently FK to orgs.id
ORG_ID_FK_TABLES = [
    "org_members",
    "invites",
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

# Domain tables where org_id becomes nullable (auth tables keep NOT NULL).
ORG_ID_NULLABLE_TABLES = [
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

# (table, column) user FKs to drop + make nullable on domain writes
USER_FK_COLUMNS = [
    ("fields", "created_by"),
    ("scouting_observations", "created_by"),
    ("jobs", "created_by"),
    ("audit_events", "user_id"),
    ("share_links", "created_by"),
    ("share_links", "revoked_by"),
]


def _drop_fk(table: str, column: str) -> None:
    """Drop FK constraint on table.column if present (name may vary)."""
    # Prefer conventional PostgreSQL name; fall back to introspection.
    conventional = f"{table}_{column}_fkey"
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
    names = {r[0] for r in rows}
    if conventional in names:
        op.drop_constraint(conventional, table, type_="foreignkey")
        names.discard(conventional)
    for name in sorted(names):
        op.drop_constraint(name, table, type_="foreignkey")


def upgrade() -> None:
    for table in ORG_ID_FK_TABLES:
        _drop_fk(table, "org_id")

    for table in ORG_ID_NULLABLE_TABLES:
        op.alter_column(
            table,
            "org_id",
            existing_type=UUID(as_uuid=True),
            nullable=True,
        )

    for table, column in USER_FK_COLUMNS:
        _drop_fk(table, column)
        # revoked_by already nullable
        if column == "revoked_by":
            continue
        op.alter_column(
            table,
            column,
            existing_type=UUID(as_uuid=True),
            nullable=True,
        )


def downgrade() -> None:
    # Best-effort reverse: re-add NOT NULL only where no NULLs exist may fail.
    # Re-create FKs; leave nullable columns as-is if data has NULLs.
    for table, column in USER_FK_COLUMNS:
        if column != "revoked_by":
            op.execute(
                sa.text(
                    f"UPDATE {table} SET {column} = "
                    f"'00000000-0000-0000-0000-000000000001'::uuid "
                    f"WHERE {column} IS NULL"
                )
            )
            op.alter_column(
                table,
                column,
                existing_type=UUID(as_uuid=True),
                nullable=False,
            )
        op.create_foreign_key(
            f"{table}_{column}_fkey",
            table,
            "users",
            [column],
            ["id"],
        )

    for table in ORG_ID_NULLABLE_TABLES:
        # Cannot safely restore NOT NULL if NULLs exist — skip alter.
        op.create_foreign_key(
            f"{table}_org_id_fkey",
            table,
            "orgs",
            ["org_id"],
            ["id"],
        )

    for table in ("org_members", "invites"):
        op.create_foreign_key(
            f"{table}_org_id_fkey",
            table,
            "orgs",
            ["org_id"],
            ["id"],
            ondelete="CASCADE",
        )
