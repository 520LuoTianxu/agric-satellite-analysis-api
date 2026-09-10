#!/usr/bin/env python3
"""Upsert OpenFarm farms/fields from agri.virtual_project_areas / land_parcels.

Product model:
  - each agri.virtual_project_areas row (tile) → public.farms
  - each agri.land_parcels row → public.fields under that farm, tagged agri:<land_id>

Deterministic IDs (uuid5 NAMESPACE_URL):
  - farm:  agri:tile:{tile_id}
  - field: agri:land:{land_id}

Usage (repo root; PG* or DATABASE_URL_SYNC from .env):

    python3 scripts/agri_seed/sync_project_areas_to_farms.py

Env:
    PGHOST / PGPORT / PGUSER / PGPASSWORD / PGDATABASE
    or DATABASE_URL_SYNC / DATABASE_URL
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
NS = uuid.NAMESPACE_URL


def load_dotenv() -> None:
    env_path = REPO / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        if key in os.environ:
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        os.environ[key] = val


def connect():
    try:
        import psycopg2
    except ImportError as exc:
        raise SystemExit("psycopg2 required: pip install psycopg2-binary") from exc

    load_dotenv()
    dsn = os.environ.get("DATABASE_URL_SYNC") or os.environ.get("DATABASE_URL")
    if dsn:
        dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
        dsn = dsn.replace("postgresql+psycopg2://", "postgresql://", 1)
        return psycopg2.connect(dsn)

    host = os.environ.get("PGHOST") or os.environ.get("DATABASE_HOST", "127.0.0.1")
    port = os.environ.get("PGPORT") or os.environ.get("DATABASE_PORT", "5432")
    user = os.environ.get("PGUSER") or os.environ.get("POSTGRES_USER", "openfarm")
    password = os.environ.get("PGPASSWORD") or os.environ.get("POSTGRES_PASSWORD", "")
    dbname = os.environ.get("PGDATABASE") or os.environ.get("POSTGRES_DB", "openfarm")
    return psycopg2.connect(
        host=host, port=port, user=user, password=password, dbname=dbname
    )


def pick_name(*parts: Any) -> str:
    for p in parts:
        if p is None:
            continue
        s = str(p).strip()
        if s:
            return s
    return "unnamed"


def farm_id_for(tile_id: str) -> uuid.UUID:
    return uuid.uuid5(NS, f"agri:tile:{tile_id}")


def field_id_for(land_id: str) -> uuid.UUID:
    return uuid.uuid5(NS, f"agri:land:{land_id}")


def sync_farms(cur) -> tuple[int, int, int]:
    cur.execute(
        """
        SELECT tile_id, group_name, county_name, city_name, project_key, province_name
        FROM agri.virtual_project_areas
        ORDER BY tile_id
        """
    )
    rows = cur.fetchall()
    inserted = updated = skipped = 0
    for tile_id, group_name, county_name, city_name, project_key, province_name in rows:
        fid = farm_id_for(tile_id)
        name = pick_name(group_name, county_name, city_name, project_key, tile_id)
        region = (county_name or "").strip() or None
        country = (province_name or "").strip() or None
        try:
            cur.execute("SAVEPOINT farm_row")
            cur.execute(
                """
                INSERT INTO farms (id, name, country, region, timezone)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    name = CASE
                        WHEN farms.name IS NULL OR btrim(farms.name) = '' THEN EXCLUDED.name
                        ELSE farms.name
                    END,
                    country = COALESCE(farms.country, EXCLUDED.country),
                    region = COALESCE(farms.region, EXCLUDED.region),
                    updated_at = now()
                RETURNING (xmax = 0) AS was_inserted
                """,
                (str(fid), name, country, region, "Asia/Shanghai"),
            )
            row = cur.fetchone()
            cur.execute("RELEASE SAVEPOINT farm_row")
            if row and row[0]:
                inserted += 1
            else:
                updated += 1
        except Exception as exc:
            cur.execute("ROLLBACK TO SAVEPOINT farm_row")
            skipped += 1
            if skipped <= 20:
                print(f"[skip-farm] tile={tile_id}: {exc}", file=sys.stderr)
    return inserted, updated, skipped


def sync_fields(cur) -> tuple[int, int, int]:
    cur.execute(
        """
        SELECT land_id, tile_id, land_name, land_area_mu, boundary_geojson
        FROM agri.land_parcels
        ORDER BY land_id
        """
    )
    rows = cur.fetchall()
    inserted = skipped_dup = skipped_err = 0
    for i, (land_id, tile_id, land_name, land_area_mu, boundary_geojson) in enumerate(
        rows, 1
    ):
        fid = field_id_for(land_id)
        farm_uuid = farm_id_for(tile_id)
        name = pick_name(land_name, land_id)
        area_ha = None
        if land_area_mu is not None:
            try:
                area_ha = float(land_area_mu) / 15.0
            except (TypeError, ValueError):
                area_ha = None
        tags = json.dumps([f"agri:{land_id}"])
        if isinstance(boundary_geojson, (dict, list)):
            geom_json = json.dumps(boundary_geojson)
        else:
            geom_json = boundary_geojson
        try:
            cur.execute("SAVEPOINT field_row")
            cur.execute(
                """
                INSERT INTO fields (
                    id, farm_id, name, geom, area_ha, crop_type, tags_json
                )
                VALUES (
                    %s, %s, %s,
                    ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)),
                    %s, %s, %s::jsonb
                )
                ON CONFLICT (id) DO NOTHING
                RETURNING id
                """,
                (
                    str(fid),
                    str(farm_uuid),
                    name,
                    geom_json,
                    area_ha,
                    "unknown",
                    tags,
                ),
            )
            got = cur.fetchone()
            cur.execute("RELEASE SAVEPOINT field_row")
            if got:
                inserted += 1
            else:
                skipped_dup += 1
        except Exception as exc:
            cur.execute("ROLLBACK TO SAVEPOINT field_row")
            skipped_err += 1
            if skipped_err <= 30:
                print(f"[skip-field] land={land_id}: {exc}", file=sys.stderr)
        if i % 1000 == 0:
            print(f"[progress] fields {i}/{len(rows)} inserted={inserted}", flush=True)
    return inserted, skipped_dup, skipped_err


def main() -> int:
    conn = connect()
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM agri.virtual_project_areas")
            n_tiles = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM agri.land_parcels")
            n_lands = cur.fetchone()[0]
            print(f"agri tiles={n_tiles} parcels={n_lands}")

            fi, fu, fs = sync_farms(cur)
            conn.commit()
            print(f"farms: inserted={fi} updated/existing={fu} skipped_err={fs}")

            li, ld, le = sync_fields(cur)
            conn.commit()
            print(f"fields: inserted={li} skipped_dup={ld} skipped_err={le}")

            cur.execute("SELECT count(*) FROM farms WHERE deleted_at IS NULL")
            farms_total = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM fields WHERE deleted_at IS NULL")
            fields_total = cur.fetchone()[0]
            cur.execute(
                """
                SELECT count(*) FROM fields f
                WHERE f.deleted_at IS NULL
                  AND EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(
                      COALESCE(f.tags_json, '[]'::jsonb)
                    ) t WHERE t LIKE 'agri:%%'
                  )
                """
            )
            agri_fields = cur.fetchone()[0]
            print(
                f"totals: farms={farms_total} fields={fields_total} "
                f"agri_tagged_fields={agri_fields}"
            )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
