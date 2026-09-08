#!/usr/bin/env python3
"""Bridge agri.parcel_scene_products → OpenFarm raster_layers + field_stats.

For each public.fields row tagged ``agri:<land_id>``, copy S2 optical index
averages (ndvi/evi/ndmi/ndre/mndwi/cire) and S1 SAR (vv/vh) into monitoring
tables so NdviTab works without waiting for the COG pipeline.

Idempotent via ON CONFLICT on uq_raster_field_date_type, and field_stats
upsert keyed by layer_id.

Usage (from repo root, docker stack running):

    python3 scripts/agri_seed/sync_agri_scenes_to_field_stats.py
    # or:
    DATABASE_HOST=127.0.0.1 python3 scripts/agri_seed/sync_agri_scenes_to_field_stats.py

Env (defaults match local compose):
    POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB / DATABASE_HOST / DATABASE_PORT
    Or DOCKER_DB_CONTAINER (default agric-satellite-analysis-db-1) to pipe via docker exec.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# (agri column prefix, layer_type stored uppercase, satellite)
INDEX_SPECS = [
    ("ndvi", "NDVI", "S2"),
    ("evi", "EVI", "S2"),
    ("ndmi", "NDMI", "S2"),
    ("ndre", "NDRE", "S2"),
    ("mndwi", "MNDWI", "S2"),
    ("cire", "CIRE", "S2"),
    ("vv", "VV", "S1"),
    ("vh", "VH", "S1"),
]


def build_sql() -> str:
    """One SQL transaction that syncs all agri-tagged fields."""
    index_unions = []
    for col, layer_type, sat in INDEX_SPECS:
        index_unions.append(
            f"""
            SELECT f.id AS field_id,
                   f.org_id,
                   land.land_id,
                   s.date,
                   '{layer_type}'::text AS layer_type,
                   '{sat}'::text AS satellite,
                   s.{col}_avg AS mean_v,
                   s.{col}_min AS min_v,
                   s.{col}_max AS max_v,
                   s.scene_id,
                   s.cloud_cover,
                   s.parcel_cloud_cover_pct
            FROM fields f
            CROSS JOIN LATERAL (
                SELECT substring(tag FROM 6) AS land_id
                FROM jsonb_array_elements_text(COALESCE(f.tags_json, '[]'::jsonb)) AS tag
                WHERE tag LIKE 'agri:%'
                LIMIT 1
            ) land
            JOIN LATERAL (
                SELECT DISTINCT ON (p.date)
                       p.date, p.scene_id, p.cloud_cover, p.parcel_cloud_cover_pct,
                       p.{col}_avg, p.{col}_min, p.{col}_max
                FROM agri.parcel_scene_products p
                WHERE p.land_id = land.land_id
                  AND p.sensor = '{sat}'
                  AND p.{col}_avg IS NOT NULL
                ORDER BY p.date,
                         p.parcel_cloud_cover_pct ASC NULLS LAST,
                         p.cloud_cover ASC NULLS LAST,
                         p.scene_id
            ) s ON true
            WHERE f.deleted_at IS NULL
            """
        )

    union_sql = "\nUNION ALL\n".join(index_unions)

    return f"""
BEGIN;

CREATE TEMP TABLE _agri_sync AS
{union_sql};

-- Upsert raster_layers (placeholder cog_uri — no real COG yet)
INSERT INTO raster_layers (
    org_id, field_id, layer_type, satellite, date, cog_uri,
    min, max, params_json, provenance_json
)
SELECT
    s.org_id,
    s.field_id,
    s.layer_type,
    s.satellite,
    s.date,
    format('agri://land/%s/%s/%s', s.land_id, lower(s.satellite), s.date::text),
    s.min_v,
    s.max_v,
    jsonb_build_object(
        'source', 'agri.parcel_scene_products',
        'land_id', s.land_id,
        'scene_id', s.scene_id,
        'cloud_cover', s.cloud_cover,
        'parcel_cloud_cover_pct', s.parcel_cloud_cover_pct
    ),
    jsonb_build_object(
        'pipeline', 'sync_agri_scenes_to_field_stats',
        'version', '1.0.0',
        'cog', 'placeholder'
    )
FROM _agri_sync s
ON CONFLICT ON CONSTRAINT uq_raster_field_date_type
DO UPDATE SET
    min = EXCLUDED.min,
    max = EXCLUDED.max,
    cog_uri = EXCLUDED.cog_uri,
    params_json = EXCLUDED.params_json,
    provenance_json = EXCLUDED.provenance_json,
    satellite = EXCLUDED.satellite;

-- Upsert field_stats for each layer
WITH layers AS (
    SELECT rl.id AS layer_id, rl.org_id, rl.field_id, rl.date, rl.layer_type,
           s.mean_v, s.min_v, s.max_v
    FROM raster_layers rl
    JOIN _agri_sync s
      ON s.field_id = rl.field_id
     AND s.date = rl.date
     AND s.layer_type = rl.layer_type
)
UPDATE field_stats fs SET
    mean = layers.mean_v,
    median = layers.mean_v,
    min = layers.min_v,
    max = layers.max_v,
    p10 = layers.min_v,
    p90 = layers.max_v,
    quality_score = 1.0
FROM layers
WHERE fs.layer_id = layers.layer_id;

INSERT INTO field_stats (
    org_id, field_id, layer_id, date,
    mean, median, min, max, p10, p90, quality_score
)
SELECT
    layers.org_id, layers.field_id, layers.layer_id, layers.date,
    layers.mean_v, layers.mean_v, layers.min_v, layers.max_v,
    layers.min_v, layers.max_v, 1.0
FROM (
    SELECT rl.id AS layer_id, rl.org_id, rl.field_id, rl.date, rl.layer_type,
           s.mean_v, s.min_v, s.max_v
    FROM raster_layers rl
    JOIN _agri_sync s
      ON s.field_id = rl.field_id
     AND s.date = rl.date
     AND s.layer_type = rl.layer_type
) layers
WHERE NOT EXISTS (
    SELECT 1 FROM field_stats fs WHERE fs.layer_id = layers.layer_id
);

-- Summary
SELECT 'synced_rows' AS metric, count(*)::text AS value FROM _agri_sync
UNION ALL
SELECT 'fields', count(DISTINCT field_id)::text FROM _agri_sync
UNION ALL
SELECT 'raster_layers_agri', count(*)::text FROM raster_layers
  WHERE cog_uri LIKE 'agri://%'
UNION ALL
SELECT 'field_stats_total', count(*)::text FROM field_stats;

COMMIT;
"""


def run_via_docker(sql: str, container: str) -> int:
    cmd = [
        "docker",
        "exec",
        "-i",
        container,
        "psql",
        "-U",
        os.environ.get("POSTGRES_USER", "openfarm"),
        "-d",
        os.environ.get("POSTGRES_DB", "openfarm"),
        "-v",
        "ON_ERROR_STOP=1",
    ]
    print(f"Running sync via docker exec {container} …", file=sys.stderr)
    proc = subprocess.run(cmd, input=sql, text=True)
    return proc.returncode


def main() -> int:
    sql = build_sql()
    if "--print-sql" in sys.argv:
        print(sql)
        return 0

    container = os.environ.get("DOCKER_DB_CONTAINER", "agric-satellite-analysis-db-1")
    # Prefer docker when container is up
    try:
        inspect = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container],
            capture_output=True,
            text=True,
            check=False,
        )
        if inspect.returncode == 0 and inspect.stdout.strip() == "true":
            return run_via_docker(sql, container)
    except FileNotFoundError:
        pass

    # Fallback: try psql on host
    host = os.environ.get("DATABASE_HOST", "127.0.0.1")
    port = os.environ.get("DATABASE_PORT", "5432")
    user = os.environ.get("POSTGRES_USER", "openfarm")
    db = os.environ.get("POSTGRES_DB", "openfarm")
    password = os.environ.get("POSTGRES_PASSWORD", "openfarm_dev")
    env = os.environ.copy()
    env["PGPASSWORD"] = password
    cmd = [
        "psql",
        "-h",
        host,
        "-p",
        port,
        "-U",
        user,
        "-d",
        db,
        "-v",
        "ON_ERROR_STOP=1",
    ]
    print(f"Running sync via psql {host}:{port}/{db} …", file=sys.stderr)
    proc = subprocess.run(cmd, input=sql, text=True, env=env)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
