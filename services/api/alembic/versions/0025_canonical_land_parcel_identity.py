"""Make ``land_parcels.land_id`` the only parcel identity.

This is the one-time cut-over from the removed legacy ``fields`` table.  The
temporary catalog relation created below exists only inside this migration;
it is not a compatibility table and is dropped automatically at commit.

The migration deliberately keeps operational and derived tables (weather,
soil, jobs, alerts, raster statistics, and shares), but changes their parcel
foreign key from the legacy UUID to ``land_id``.  The agricultural parcel
boundary remains authoritative; the useful legacy attributes are merged into
that same row before ``fields`` is dropped.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels = None
depends_on = None

SCHEMA = "agric_satellite"
FIELD_TABLES = (
    "raster_layers",
    "field_stats",
    "alerts",
    "scouting_observations",
    "jobs",
    "share_links",
    "weather_daily",
    "soil_profiles",
    "soil_field_summary",
    "soil_nutrient_npk",
    "group_site_admission",
)
NON_NULL_LAND_ID_TABLES = (
    "raster_layers",
    "field_stats",
    "alerts",
    "scouting_observations",
    "share_links",
    "weather_daily",
    "soil_profiles",
    "soil_field_summary",
    "soil_nutrient_npk",
)


def _q(bind: sa.engine.Connection, identifier: str) -> str:
    return bind.dialect.identifier_preparer.quote(identifier)


def _qualified(bind: sa.engine.Connection, table: str) -> str:
    return f"{_q(bind, SCHEMA)}.{_q(bind, table)}"


def _table_exists(bind: sa.engine.Connection, table: str) -> bool:
    return (
        bind.execute(
            sa.text(
                """
                SELECT to_regclass(:name) IS NOT NULL
                """
            ),
            {"name": f"{SCHEMA}.{table}"},
        ).scalar()
        is True
    )


def _column_exists(bind: sa.engine.Connection, table: str, column: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = :schema
                  AND table_name = :table
                  AND column_name = :column
                """
            ),
            {"schema": SCHEMA, "table": table, "column": column},
        ).scalar()
    )


def _constraint_exists(bind: sa.engine.Connection, table: str, name: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT 1
                FROM pg_catalog.pg_constraint c
                JOIN pg_catalog.pg_class r ON r.oid = c.conrelid
                JOIN pg_catalog.pg_namespace n ON n.oid = r.relnamespace
                WHERE n.nspname = :schema
                  AND r.relname = :table
                  AND c.conname = :name
                """
            ),
            {"schema": SCHEMA, "table": table, "name": name},
        ).scalar()
    )


def _drop_constraints_containing_column(
    bind: sa.engine.Connection, table: str, column: str
) -> None:
    """Drop old FKs/unique constraints before replacing the identity column."""

    if not _table_exists(bind, table):
        return
    rows = bind.execute(
        sa.text(
            """
            SELECT c.conname
            FROM pg_catalog.pg_constraint c
            JOIN pg_catalog.pg_class r ON r.oid = c.conrelid
            JOIN pg_catalog.pg_namespace n ON n.oid = r.relnamespace
            WHERE n.nspname = :schema
              AND r.relname = :table
              AND pg_catalog.pg_get_constraintdef(c.oid) ILIKE :pattern
            ORDER BY c.conname
            """
        ),
        {"schema": SCHEMA, "table": table, "pattern": f"%{column}%"},
    ).scalars()
    qualified = _qualified(bind, table)
    for name in rows:
        bind.execute(
            sa.text(
                f"ALTER TABLE {qualified} DROP CONSTRAINT IF EXISTS {_q(bind, str(name))}"
            )
        )


def _drop_indexes_containing_column(
    bind: sa.engine.Connection, table: str, column: str
) -> None:
    """Remove indexes that would otherwise retain the legacy column name."""

    if not _table_exists(bind, table):
        return
    rows = bind.execute(
        sa.text(
            """
            SELECT DISTINCT idx.relname
            FROM pg_catalog.pg_class tbl
            JOIN pg_catalog.pg_namespace tbl_ns ON tbl_ns.oid = tbl.relnamespace
            JOIN pg_catalog.pg_index i ON i.indrelid = tbl.oid
            JOIN pg_catalog.pg_class idx ON idx.oid = i.indexrelid
            JOIN pg_catalog.pg_attribute a
              ON a.attrelid = tbl.oid
             AND a.attnum = ANY(i.indkey)
            WHERE tbl_ns.nspname = :schema
              AND tbl.relname = :table
              AND a.attname = :column
              AND NOT i.indisprimary
            ORDER BY idx.relname
            """
        ),
        {"schema": SCHEMA, "table": table, "column": column},
    ).scalars()
    for name in rows:
        bind.execute(
            sa.text(
                f"DROP INDEX IF EXISTS {SCHEMA}.{_q(bind, str(name))}"
            )
        )


def _add_land_fk(bind: sa.engine.Connection, table: str, on_delete: str) -> None:
    if not _table_exists(bind, table):
        return
    name = f"{table}_land_id_fkey"
    if _constraint_exists(bind, table, name):
        return
    bind.execute(
        sa.text(
            f"ALTER TABLE {_qualified(bind, table)} "
            f"ADD CONSTRAINT {_q(bind, name)} FOREIGN KEY (land_id) "
            f"REFERENCES {_qualified(bind, 'land_parcels')} (land_id) "
            f"ON DELETE {on_delete}"
        )
    )


def _add_index(bind: sa.engine.Connection, name: str, table: str, columns: str) -> None:
    if _table_exists(bind, table):
        bind.execute(
            sa.text(
                f"CREATE INDEX IF NOT EXISTS {_q(bind, name)} "
                f"ON {_qualified(bind, table)} ({columns})"
            )
        )


def _add_unique(
    bind: sa.engine.Connection, name: str, table: str, columns: str
) -> None:
    """Add a post-cutover uniqueness rule without failing on a seeded DB."""
    if _table_exists(bind, table) and not _constraint_exists(bind, table, name):
        bind.execute(
            sa.text(
                f"ALTER TABLE {_qualified(bind, table)} "
                f"ADD CONSTRAINT {_q(bind, name)} UNIQUE ({columns})"
            )
        )


def _merge_legacy_parcel_attributes(bind: sa.engine.Connection) -> None:
    """Convert tagged legacy rows into the one canonical parcel table."""

    bind.execute(
        sa.text(
            """
            ALTER TABLE agric_satellite.land_parcels
                ADD COLUMN IF NOT EXISTS farm_id uuid,
                ADD COLUMN IF NOT EXISTS source_parcel_id text,
                ADD COLUMN IF NOT EXISTS tile_id text,
                ADD COLUMN IF NOT EXISTS virtual_tile_id text,
                ADD COLUMN IF NOT EXISTS project_key text,
                ADD COLUMN IF NOT EXISTS tile_assignment_type text,
                ADD COLUMN IF NOT EXISTS tile_anchor_land_id text,
                ADD COLUMN IF NOT EXISTS group_id text,
                ADD COLUMN IF NOT EXISTS group_name text,
                ADD COLUMN IF NOT EXISTS org_code text,
                ADD COLUMN IF NOT EXISTS org_name text,
                ADD COLUMN IF NOT EXISTS base_id text,
                ADD COLUMN IF NOT EXISTS province_code text,
                ADD COLUMN IF NOT EXISTS province_name text,
                ADD COLUMN IF NOT EXISTS city_code text,
                ADD COLUMN IF NOT EXISTS city_name text,
                ADD COLUMN IF NOT EXISTS county_code text,
                ADD COLUMN IF NOT EXISTS county_name text,
                ADD COLUMN IF NOT EXISTS town_code text,
                ADD COLUMN IF NOT EXISTS town_name text,
                ADD COLUMN IF NOT EXISTS village_code text,
                ADD COLUMN IF NOT EXISTS village_name text,
                ADD COLUMN IF NOT EXISTS geom geometry(MULTIPOLYGON,4326),
                ADD COLUMN IF NOT EXISTS area_ha numeric(12,4),
                ADD COLUMN IF NOT EXISTS crop_type text,
                ADD COLUMN IF NOT EXISTS season text,
                ADD COLUMN IF NOT EXISTS tags_json jsonb,
                ADD COLUMN IF NOT EXISTS soil_property text,
                ADD COLUMN IF NOT EXISTS current_batch text,
                ADD COLUMN IF NOT EXISTS land_status text,
                ADD COLUMN IF NOT EXISTS source_update_time timestamp,
                ADD COLUMN IF NOT EXISTS source_file text,
                ADD COLUMN IF NOT EXISTS source_feature_index integer,
                ADD COLUMN IF NOT EXISTS deleted_at timestamptz
            """
        )
    )

    # Choose one source row only for attributes.  All dependent rows below
    # use every tagged field row, so duplicate legacy rows cannot lose data.
    bind.execute(
        sa.text(
            """
            CREATE TEMP TABLE _legacy_land_sources ON COMMIT DROP AS
            WITH tagged AS (
                SELECT
                    f.id AS field_id,
                    f.farm_id,
                    f.name,
                    f.geom,
                    f.area_ha,
                    f.crop_type,
                    f.season,
                    f.tags_json,
                    btrim(substring(t.tag FROM 6)) AS land_id,
                    f.updated_at
                FROM agric_satellite.fields f
                CROSS JOIN LATERAL jsonb_array_elements_text(
                    CASE
                        WHEN jsonb_typeof(COALESCE(f.tags_json, '[]'::jsonb)) = 'array'
                        THEN COALESCE(f.tags_json, '[]'::jsonb)
                        ELSE '[]'::jsonb
                    END
                ) AS t(tag)
                WHERE f.deleted_at IS NULL
                  AND t.tag LIKE 'agri:%'
                  AND btrim(substring(t.tag FROM 6)) <> ''
            )
            SELECT DISTINCT ON (land_id)
                field_id, farm_id, name, geom, area_ha, crop_type, season,
                tags_json, land_id, updated_at
            FROM tagged
            ORDER BY land_id, updated_at DESC NULLS LAST, field_id::text DESC
            """
        )
    )
    # Older application-created parcel rows did not have the agricultural
    # source columns.  Fill safe provenance defaults before enforcing the
    # canonical table's required source metadata.
    bind.execute(
        sa.text(
            """
            UPDATE agric_satellite.land_parcels
            SET source_parcel_id = COALESCE(NULLIF(source_parcel_id, ''), land_id),
                tile_id = COALESCE(NULLIF(tile_id, ''), 'legacy_' || land_id),
                source_file = COALESCE(NULLIF(source_file, ''), 'canonical_migration'),
                source_feature_index = COALESCE(source_feature_index, 0)
            """
        )
    )
    bind.execute(
        sa.text(
            """
            ALTER TABLE agric_satellite.land_parcels
                ALTER COLUMN tile_id SET NOT NULL,
                ALTER COLUMN source_file SET NOT NULL,
                ALTER COLUMN source_feature_index SET NOT NULL
            """
        )
    )

    missing_geom = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM _legacy_land_sources
            WHERE geom IS NULL OR ST_IsEmpty(geom)
            """
        )
    ).scalar()
    if missing_geom:
        raise RuntimeError(
            f"cannot canonicalize {missing_geom} tagged parcel(s) without geometry"
        )

    # 25106 is present in the legacy table but absent from the current agri
    # import.  Create it directly as a canonical land row; no mapping survives.
    bind.execute(
        sa.text(
            """
            INSERT INTO agric_satellite.land_parcels (
                land_id, source_parcel_id, tile_id, land_name, farm_id,
                original_area_mu, land_area_mu, boundary_geojson, boundary_srid,
                min_lon, min_lat, max_lon, max_lat, source_properties,
                source_file, source_feature_index, created_at, updated_at
            )
            SELECT
                s.land_id,
                s.land_id,
                'legacy_' || s.land_id,
                s.name,
                s.farm_id,
                round((s.area_ha * 15.0)::numeric, 4),
                round((s.area_ha * 15.0)::numeric, 4),
                ST_AsGeoJSON(ST_Multi(ST_Force2D(s.geom)))::jsonb,
                4326,
                ST_XMin(ST_Envelope(s.geom)::box3d),
                ST_YMin(ST_Envelope(s.geom)::box3d),
                ST_XMax(ST_Envelope(s.geom)::box3d),
                ST_YMax(ST_Envelope(s.geom)::box3d),
                jsonb_build_object(
                    'source', 'legacy_fields_migration',
                    'legacy_tags', COALESCE(s.tags_json, '[]'::jsonb)
                ),
                'legacy_fields_migration',
                0,
                now(),
                now()
            FROM _legacy_land_sources s
            WHERE NOT EXISTS (
                SELECT 1
                FROM agric_satellite.land_parcels p
                WHERE p.land_id = s.land_id
            )
            """
        )
    )

    # The agri boundary is authoritative.  Only useful legacy business
    # attributes are merged into the same row; no second parcel identity is
    # introduced.
    bind.execute(
        sa.text(
            """
            UPDATE agric_satellite.land_parcels p
            SET farm_id = COALESCE(p.farm_id, s.farm_id),
                area_ha = COALESCE(p.area_ha, s.area_ha),
                crop_type = COALESCE(p.crop_type, s.crop_type),
                season = COALESCE(p.season, s.season),
                tags_json = COALESCE(p.tags_json, s.tags_json),
                geom = COALESCE(
                    p.geom,
                    ST_Multi(ST_SetSRID(
                        ST_GeomFromGeoJSON(p.boundary_geojson::text), 4326
                    ))
                ),
                updated_at = now()
            FROM _legacy_land_sources s
            WHERE p.land_id = s.land_id
            """
        )
    )
    bind.execute(
        sa.text(
            """
            UPDATE agric_satellite.land_parcels p
            SET geom = ST_Multi(ST_SetSRID(
                    ST_GeomFromGeoJSON(p.boundary_geojson::text), 4326
                )),
                updated_at = now()
            WHERE p.geom IS NULL
            """
        )
    )

    # This is a transaction-local conversion relation, never an application
    # table.  It is used only while child rows are being re-keyed.
    bind.execute(
        sa.text(
            """
            CREATE TEMP TABLE _legacy_field_land ON COMMIT DROP AS
            SELECT
                f.id AS field_id,
                s.land_id
            FROM agric_satellite.fields f
            JOIN _legacy_land_sources s
              ON s.land_id = (
                  SELECT btrim(substring(t.tag FROM 6))
                  FROM jsonb_array_elements_text(
                      CASE
                          WHEN jsonb_typeof(COALESCE(f.tags_json, '[]'::jsonb)) = 'array'
                          THEN COALESCE(f.tags_json, '[]'::jsonb)
                          ELSE '[]'::jsonb
                      END
                  ) AS t(tag)
                  WHERE t.tag LIKE 'agri:%'
                    AND btrim(substring(t.tag FROM 6)) <> ''
                  ORDER BY t.tag
                  LIMIT 1
              )
            WHERE f.deleted_at IS NULL
            """
        )
    )


def _rekey_child_tables(bind: sa.engine.Connection) -> None:
    """Replace child UUID keys with direct text land_id keys."""

    for table in FIELD_TABLES:
        if not _table_exists(bind, table) or not _column_exists(bind, table, "field_id"):
            continue
        if not _column_exists(bind, table, "land_id"):
            bind.execute(
                sa.text(
                    f"ALTER TABLE {_qualified(bind, table)} "
                    "ADD COLUMN land_id text"
                )
            )
        bind.execute(
            sa.text(
                f"UPDATE {_qualified(bind, table)} child "
                "SET land_id = COALESCE(NULLIF(child.land_id, ''), m.land_id) "
                "FROM _legacy_field_land m "
                "WHERE child.field_id = m.field_id"
            )
        )

    # Legacy classic products without an explicit agri land identity are
    # intentionally discarded: keeping them would recreate the duplicate
    # OpenFarm parcel set the cut-over is removing.
    for table in NON_NULL_LAND_ID_TABLES:
        if _table_exists(bind, table) and _column_exists(bind, table, "land_id"):
            bind.execute(
                sa.text(
                    f"DELETE FROM {_qualified(bind, table)} "
                    "WHERE land_id IS NULL OR btrim(land_id) = ''"
                )
            )
    if _table_exists(bind, "jobs") and _column_exists(bind, "jobs", "land_id"):
        bind.execute(
            sa.text(
                f"DELETE FROM {_qualified(bind, 'jobs')} "
                "WHERE field_id IS NOT NULL AND (land_id IS NULL OR btrim(land_id) = '')"
            )
        )

    if _table_exists(bind, "mq_task_results"):
        if not _column_exists(bind, "mq_task_results", "land_id"):
            bind.execute(
                sa.text(
                    f"ALTER TABLE {_qualified(bind, 'mq_task_results')} "
                    "ADD COLUMN land_id text"
                )
            )
        if _column_exists(bind, "mq_task_results", "field_id"):
            bind.execute(
                sa.text(
                    f"UPDATE {_qualified(bind, 'mq_task_results')} result "
                    "SET land_id = COALESCE(NULLIF(result.land_id, ''), m.land_id) "
                    "FROM _legacy_field_land m "
                    "WHERE result.field_id = m.field_id::text"
                )
            )

    if _table_exists(bind, "work_items") and _column_exists(
        bind, "work_items", "payload_json"
    ):
        # Convert queued work once, then remove the old JSON key.  Unknown
        # legacy work is failed rather than silently dispatched without a land.
        bind.execute(
            sa.text(
                f"UPDATE {_qualified(bind, 'work_items')} item "
                "SET payload_json = jsonb_set("
                "item.payload_json - 'field_id', '{land_id}', "
                "to_jsonb(m.land_id), true) "
                "FROM _legacy_field_land m "
                "WHERE item.payload_json->>'field_id' = m.field_id::text"
            )
        )
        bind.execute(
            sa.text(
                f"UPDATE {_qualified(bind, 'work_items')} "
                "SET status = 'failed', "
                "error = 'legacy field work item dropped: land_id required' "
                "WHERE payload_json ? 'field_id'"
            )
        )


def _deduplicate_rekeyed_rows(bind: sa.engine.Connection) -> None:
    """Resolve collisions caused by duplicate legacy rows before constraints."""

    if _table_exists(bind, "raster_layers"):
        bind.execute(
            sa.text(
                """
                WITH ranked AS (
                    SELECT id,
                           first_value(id) OVER (
                               PARTITION BY land_id, date, layer_type
                               ORDER BY created_at DESC NULLS LAST, id DESC
                           ) AS keeper,
                           row_number() OVER (
                               PARTITION BY land_id, date, layer_type
                               ORDER BY created_at DESC NULLS LAST, id DESC
                           ) AS rn
                    FROM agric_satellite.raster_layers
                )
                UPDATE agric_satellite.field_stats fs
                SET layer_id = r.keeper
                FROM ranked r
                WHERE fs.layer_id = r.id AND r.rn > 1
                """
            )
        )
        bind.execute(
            sa.text(
                """
                DELETE FROM agric_satellite.raster_layers layer
                USING (
                    SELECT id,
                           row_number() OVER (
                               PARTITION BY land_id, date, layer_type
                               ORDER BY created_at DESC NULLS LAST, id DESC
                           ) AS rn
                    FROM agric_satellite.raster_layers
                ) duplicate
                WHERE layer.id = duplicate.id AND duplicate.rn > 1
                """
            )
        )

    if _table_exists(bind, "weather_daily"):
        bind.execute(
            sa.text(
                """
                DELETE FROM agric_satellite.weather_daily row_to_delete
                USING (
                    SELECT id,
                           row_number() OVER (
                               PARTITION BY land_id, date
                               ORDER BY updated_at DESC NULLS LAST, id DESC
                           ) AS rn
                    FROM agric_satellite.weather_daily
                ) duplicate
                WHERE row_to_delete.id = duplicate.id AND duplicate.rn > 1
                """
            )
        )

    if _table_exists(bind, "soil_profiles"):
        bind.execute(
            sa.text(
                """
                WITH ranked AS (
                    SELECT id,
                           first_value(id) OVER (
                               PARTITION BY land_id, source
                               ORDER BY fetched_at DESC NULLS LAST, id DESC
                           ) AS keeper,
                           row_number() OVER (
                               PARTITION BY land_id, source
                               ORDER BY fetched_at DESC NULLS LAST, id DESC
                           ) AS rn
                    FROM agric_satellite.soil_profiles
                )
                UPDATE agric_satellite.soil_layers layer
                SET profile_id = r.keeper
                FROM ranked r
                WHERE layer.profile_id = r.id AND r.rn > 1
                """
            )
        )
        bind.execute(
            sa.text(
                """
                UPDATE agric_satellite.soil_field_summary summary
                SET profile_id = ranked.keeper
                FROM (
                    SELECT id,
                           first_value(id) OVER (
                               PARTITION BY land_id, source
                               ORDER BY fetched_at DESC NULLS LAST, id DESC
                           ) AS keeper,
                           row_number() OVER (
                               PARTITION BY land_id, source
                               ORDER BY fetched_at DESC NULLS LAST, id DESC
                           ) AS rn
                    FROM agric_satellite.soil_profiles
                ) ranked
                WHERE summary.profile_id = ranked.id AND ranked.rn > 1
                """
            )
        )
        bind.execute(
            sa.text(
                """
                DELETE FROM agric_satellite.soil_profiles profile
                USING (
                    SELECT id,
                           row_number() OVER (
                               PARTITION BY land_id, source
                               ORDER BY fetched_at DESC NULLS LAST, id DESC
                           ) AS rn
                    FROM agric_satellite.soil_profiles
                ) duplicate
                WHERE profile.id = duplicate.id AND duplicate.rn > 1
                """
            )
        )

    if _table_exists(bind, "soil_field_summary"):
        bind.execute(
            sa.text(
                """
                DELETE FROM agric_satellite.soil_field_summary summary
                USING (
                    SELECT id,
                           row_number() OVER (
                               PARTITION BY land_id
                               ORDER BY computed_at DESC NULLS LAST, id DESC
                           ) AS rn
                    FROM agric_satellite.soil_field_summary
                ) duplicate
                WHERE summary.id = duplicate.id AND duplicate.rn > 1
                """
            )
        )

    if _table_exists(bind, "soil_nutrient_npk"):
        bind.execute(
            sa.text(
                """
                DELETE FROM agric_satellite.soil_nutrient_npk row_to_delete
                USING (
                    SELECT id,
                           row_number() OVER (
                               PARTITION BY land_id
                               ORDER BY updated_at DESC NULLS LAST, id DESC
                           ) AS rn
                    FROM agric_satellite.soil_nutrient_npk
                ) duplicate
                WHERE row_to_delete.id = duplicate.id AND duplicate.rn > 1
                """
            )
        )


def _finish_child_schema(bind: sa.engine.Connection) -> None:
    for table in FIELD_TABLES:
        if not _table_exists(bind, table) or not _column_exists(bind, table, "field_id"):
            continue
        _drop_constraints_containing_column(bind, table, "field_id")
        _drop_indexes_containing_column(bind, table, "field_id")
        bind.execute(
            sa.text(
                f"ALTER TABLE {_qualified(bind, table)} DROP COLUMN field_id"
            )
        )

    if _table_exists(bind, "mq_task_results") and _column_exists(
        bind, "mq_task_results", "field_id"
    ):
        _drop_indexes_containing_column(bind, "mq_task_results", "field_id")
        bind.execute(
            sa.text(
                f"ALTER TABLE {_qualified(bind, 'mq_task_results')} DROP COLUMN field_id"
            )
        )

    # ``fields`` itself is intentionally not replaced by a view or alias.
    if _table_exists(bind, "fields"):
        bind.execute(sa.text(f"DROP TABLE {_qualified(bind, 'fields')}") )

    if not _constraint_exists(bind, "land_parcels", "land_parcels_farm_id_fkey"):
        bind.execute(
            sa.text(
                """
                ALTER TABLE agric_satellite.land_parcels
                    ADD CONSTRAINT land_parcels_farm_id_fkey
                    FOREIGN KEY (farm_id)
                    REFERENCES agric_satellite.farms(id)
                    ON DELETE SET NULL
                """
            )
        )

    for table in NON_NULL_LAND_ID_TABLES:
        if _table_exists(bind, table):
            bind.execute(
                sa.text(
                    f"ALTER TABLE {_qualified(bind, table)} "
                    "ALTER COLUMN land_id SET NOT NULL"
                )
            )

    for table in FIELD_TABLES:
        if not _table_exists(bind, table):
            continue
        on_delete = "SET NULL" if table == "group_site_admission" else "CASCADE"
        _add_land_fk(bind, table, on_delete)

    _add_unique(
        bind,
        "uq_raster_land_date_type",
        "raster_layers",
        "land_id, date, layer_type",
    )
    _add_unique(bind, "uq_weather_land_date", "weather_daily", "land_id, date")
    _add_unique(
        bind,
        "uq_soil_land_summary_land",
        "soil_field_summary",
        "land_id",
    )
    _add_unique(
        bind,
        "uq_soil_nutrient_npk_land",
        "soil_nutrient_npk",
        "land_id",
    )

    _add_index(bind, "idx_raster_layers_land_id", "raster_layers", "land_id")
    _add_index(bind, "idx_field_stats_land_id", "field_stats", "land_id")
    _add_index(bind, "idx_alerts_land_id", "alerts", "land_id")
    _add_index(bind, "idx_scouting_land_id", "scouting_observations", "land_id")
    _add_index(bind, "idx_jobs_land_id", "jobs", "land_id")
    _add_index(bind, "idx_share_links_land_id", "share_links", "land_id")
    _add_index(bind, "idx_weather_land_date", "weather_daily", "land_id, date")
    _add_index(bind, "idx_soil_profiles_land_id", "soil_profiles", "land_id")
    _add_index(bind, "idx_soil_land_summary_land_id", "soil_field_summary", "land_id")
    _add_index(bind, "idx_soil_nutrient_npk_land_id", "soil_nutrient_npk", "land_id")
    _add_index(bind, "idx_group_site_admission_land_id", "group_site_admission", "land_id")
    _add_index(bind, "idx_land_parcels_farm_id", "land_parcels", "farm_id")

    bind.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS land_parcels_geom_idx "
            "ON agric_satellite.land_parcels USING GIST (geom)"
        )
    )

    # Keep updated_at in the same schema as the canonical master when the
    # shared trigger function is available.
    has_trigger_fn = bind.execute(
        sa.text("SELECT to_regprocedure('agric_satellite.set_updated_at()')")
    ).scalar()
    if has_trigger_fn:
        bind.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS trg_land_parcels_updated_at "
                "ON agric_satellite.land_parcels"
            )
        )
        bind.execute(
            sa.text(
                "CREATE TRIGGER trg_land_parcels_updated_at "
                "BEFORE UPDATE ON agric_satellite.land_parcels "
                "FOR EACH ROW EXECUTE FUNCTION agric_satellite.set_updated_at()"
            )
        )


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '30s'")
    op.execute("SELECT pg_advisory_xact_lock(hashtext('0025_canonical_land_parcel_identity'))")

    if not _table_exists(bind, "land_parcels") or not _table_exists(bind, "fields"):
        raise RuntimeError(
            "0025 requires agric_satellite.land_parcels and agric_satellite.fields"
        )

    _merge_legacy_parcel_attributes(bind)
    _rekey_child_tables(bind)
    _deduplicate_rekeyed_rows(bind)
    _finish_child_schema(bind)


def downgrade() -> None:
    raise RuntimeError(
        "0025 is irreversible: the legacy fields table and UUID parcel identity "
        "were intentionally removed"
    )
