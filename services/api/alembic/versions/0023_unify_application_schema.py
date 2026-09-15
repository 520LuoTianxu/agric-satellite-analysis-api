"""Move all application tables into the single ``agric_satellite`` schema.

At this step PostGIS-owned objects and the Alembic version table remain in
``public``; revision 0024 completes their move and removes that schema.  This
migration only changes PostgreSQL namespaces, so table data, indexes,
constraints, and OIDs are preserved without a bulk copy.
"""

from __future__ import annotations

from typing import Iterable

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels = None
depends_on = None

APP_SCHEMA = "agric_satellite"
LEGACY_AGRI_SCHEMA = "agri"

# Former public application tables.  Keep this allowlist explicit
# so PostGIS metadata in public can never be moved by accident at this step.
PUBLIC_APPLICATION_TABLES = (
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
    "soil_layers",
    "soil_field_summary",
    "soil_nutrient_npk",
    "group_site_admission",
    "work_items",
)

# Relations created by the agri seed scripts.  The upgrade discovers the
# actual catalog relations so newly-added agri tables/views are moved too;
# these names make downgrade deterministic and auditable.
LEGACY_AGRI_TABLES = (
    "ingest_batch_stats",
    "ingest_runs",
    "ingested_oss_objects",
    "land_parcels",
    "mq_task_results",
    "overview_stats_daily",
    "parcel_scene_products",
    "virtual_project_area_lands",
    "virtual_project_areas",
)
LEGACY_AGRI_VIEWS = (
    "v_land_parcels_detail",
    "v_parcel_scene_products_meta",
    "v_virtual_project_areas_detail",
)
LEGACY_AGRI_SEQUENCES = (
    "ingest_batch_stats_batch_stat_id_seq",
    "ingest_runs_run_id_seq",
)


def _quote(bind: sa.engine.Connection, identifier: str) -> str:
    """Quote a catalog identifier before composing an ALTER statement."""

    return bind.dialect.identifier_preparer.quote(identifier)


def _relation_exists(bind: sa.engine.Connection, schema: str, name: str) -> bool:
    qualified = f"{schema}.{name}"
    return (
        bind.execute(
            sa.text("SELECT to_regclass(:qualified)"), {"qualified": qualified}
        ).scalar()
        is not None
    )


def _move_relation(
    bind: sa.engine.Connection,
    source_schema: str,
    target_schema: str,
    name: str,
    relation_kind: str,
) -> None:
    """Move one relation and refuse to overwrite an existing target object."""

    source_exists = _relation_exists(bind, source_schema, name)
    target_exists = _relation_exists(bind, target_schema, name)
    if not source_exists:
        return
    if target_exists:
        raise RuntimeError(
            f"schema migration collision: both {source_schema}.{name} "
            f"and {target_schema}.{name} exist"
        )

    source = f"{_quote(bind, source_schema)}.{_quote(bind, name)}"
    target = _quote(bind, target_schema)
    bind.execute(sa.text(f"ALTER {relation_kind} {source} SET SCHEMA {target}"))


def _catalog_relations(
    bind: sa.engine.Connection, schema: str, relation_kinds: Iterable[str]
) -> list[tuple[str, str]]:
    rows = bind.execute(
        sa.text(
            """
            SELECT c.relname, c.relkind
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = :schema
              AND c.relkind::text = ANY(CAST(:relation_kinds AS text[]))
            ORDER BY c.relkind, c.relname
            """
        ),
        {"schema": schema, "relation_kinds": list(relation_kinds)},
    ).all()
    return [(str(name), str(kind)) for name, kind in rows]


def _move_legacy_agri_relations(
    bind: sa.engine.Connection, source_schema: str, target_schema: str
) -> None:
    # 表先移动，序列随后移动，视图最后移动，避免旧 schema 中残留对象。
    kind_order = (
        ("r", "TABLE"),
        ("p", "TABLE"),
        ("f", "FOREIGN TABLE"),
        ("S", "SEQUENCE"),
        ("v", "VIEW"),
        ("m", "MATERIALIZED VIEW"),
    )
    for relkind, ddl_kind in kind_order:
        for name, _ in _catalog_relations(bind, source_schema, (relkind,)):
            _move_relation(bind, source_schema, target_schema, name, ddl_kind)


def _move_public_owned_sequences(
    bind: sa.engine.Connection, target_schema: str
) -> None:
    """Move serial sequences owned by the allowlisted public tables."""

    rows = bind.execute(
        sa.text(
            """
            SELECT DISTINCT seq.relname
            FROM pg_catalog.pg_class AS seq
            JOIN pg_catalog.pg_namespace AS seq_ns ON seq_ns.oid = seq.relnamespace
            JOIN pg_catalog.pg_depend AS dep
              ON dep.classid = 'pg_catalog.pg_class'::regclass
             AND dep.objid = seq.oid
             AND dep.deptype = 'a'
            JOIN pg_catalog.pg_class AS tbl ON tbl.oid = dep.refobjid
            JOIN pg_catalog.pg_namespace AS tbl_ns ON tbl_ns.oid = tbl.relnamespace
            WHERE seq.relkind = 'S'
              AND seq_ns.nspname = 'public'
              AND tbl_ns.nspname = 'public'
              AND tbl.relname::text = ANY(CAST(:table_names AS text[]))
            ORDER BY seq.relname
            """
        ),
        {"table_names": list(PUBLIC_APPLICATION_TABLES)},
    ).scalars()
    for name in rows:
        _move_relation(bind, "public", target_schema, str(name), "SEQUENCE")


def _move_updated_at_function(
    bind: sa.engine.Connection, source_schema: str, target_schema: str
) -> None:
    """Keep the trigger function beside the application tables."""

    source_exists = (
        bind.execute(
            sa.text("SELECT to_regprocedure(:qualified)"),
            {"qualified": f"{source_schema}.set_updated_at()"},
        ).scalar()
        is not None
    )
    target_exists = (
        bind.execute(
            sa.text("SELECT to_regprocedure(:qualified)"),
            {"qualified": f"{target_schema}.set_updated_at()"},
        ).scalar()
        is not None
    )
    if not source_exists:
        return
    if target_exists:
        raise RuntimeError(
            "schema migration collision: both schemas contain set_updated_at()"
        )
    bind.execute(
        sa.text(
            "ALTER FUNCTION "
            f"{_quote(bind, source_schema)}.set_updated_at() "
            f"SET SCHEMA {_quote(bind, target_schema)}"
        )
    )


def _assert_schema_empty(bind: sa.engine.Connection, schema: str) -> None:
    rows = bind.execute(
        sa.text(
            """
            SELECT 'relation' AS object_type, c.relname AS object_name
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = :schema
            UNION ALL
            SELECT 'function' AS object_type, p.proname AS object_name
            FROM pg_catalog.pg_proc AS p
            JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
            WHERE n.nspname = :schema
            ORDER BY object_type, object_name
            """
        ),
        {"schema": schema},
    ).all()
    if rows:
        formatted = ", ".join(f"{kind}:{name}" for kind, name in rows)
        raise RuntimeError(f"legacy schema {schema} is not empty: {formatted}")


def _move_known_relations_back(
    bind: sa.engine.Connection, source_schema: str, target_schema: str
) -> None:
    # Reverse the upgrade with explicit names.  The downgrade is intentionally
    # best-effort for tables that were created after this migration was written.
    for name in LEGACY_AGRI_TABLES:
        _move_relation(bind, source_schema, target_schema, name, "TABLE")
    for name in LEGACY_AGRI_SEQUENCES:
        _move_relation(bind, source_schema, target_schema, name, "SEQUENCE")
    for name in LEGACY_AGRI_VIEWS:
        _move_relation(bind, source_schema, target_schema, name, "VIEW")


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("CREATE SCHEMA IF NOT EXISTS agric_satellite")
    # SET SCHEMA 只改系统目录，锁持有时间应短；若有写入任务未停，直接失败并回滚。
    op.execute("SET LOCAL lock_timeout = '30s'")

    _move_public_owned_sequences(bind, APP_SCHEMA)
    for name in PUBLIC_APPLICATION_TABLES:
        _move_relation(bind, "public", APP_SCHEMA, name, "TABLE")

    _move_legacy_agri_relations(bind, LEGACY_AGRI_SCHEMA, APP_SCHEMA)
    _move_updated_at_function(bind, "public", APP_SCHEMA)

    _assert_schema_empty(bind, LEGACY_AGRI_SCHEMA)
    op.execute("DROP SCHEMA IF EXISTS agri")


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("CREATE SCHEMA IF NOT EXISTS agri")
    op.execute("SET LOCAL lock_timeout = '30s'")

    _move_known_relations_back(bind, APP_SCHEMA, LEGACY_AGRI_SCHEMA)
    for name in reversed(PUBLIC_APPLICATION_TABLES):
        _move_relation(bind, APP_SCHEMA, "public", name, "TABLE")
    _move_updated_at_function(bind, APP_SCHEMA, "public")

    _assert_schema_empty(bind, APP_SCHEMA)
    op.execute("DROP SCHEMA IF EXISTS agric_satellite")
