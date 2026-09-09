#!/usr/bin/env python3
"""Ensure agri-tagged OpenFarm fields have parcel geom + soil/weather.

For each public.fields row with tags containing ``agri:<land_id>``:

1. If geom is missing/empty, copy boundary from agri.land_parcels.boundary_geojson.
2. If no soil_field_summary row, enqueue fetch_soil_for_field (or print SQL hint).
3. If no weather_daily rows, enqueue backfill_weather_for_field.

Default: only farm named 「范莘·lonlat_v1样例」 (override with --farm / --all).

Usage (repo root, compose up):

    # Dry-run (default): print what would happen
    python3 scripts/agri_seed/ensure_agri_field_soil_weather.py

    # Apply geom fixes via psql + dispatch Celery through api container:
    python3 scripts/agri_seed/ensure_agri_field_soil_weather.py --apply

    # All agri-tagged fields:
    python3 scripts/agri_seed/ensure_agri_field_soil_weather.py --apply --all
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

DEFAULT_FARM = "范莘·lonlat_v1样例"
DB_CONTAINER = os.environ.get("DOCKER_DB_CONTAINER", "agric-satellite-analysis-db-1")
API_CONTAINER = os.environ.get("DOCKER_API_CONTAINER", "agric-satellite-analysis-api-1")
PGUSER = os.environ.get("POSTGRES_USER", "openfarm")
PGDB = os.environ.get("POSTGRES_DB", "openfarm")


def psql(sql: str) -> str:
    r = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            DB_CONTAINER,
            "psql",
            "-U",
            PGUSER,
            "-d",
            PGDB,
            "-v",
            "ON_ERROR_STOP=1",
            "-t",
            "-A",
            "-F",
            "\t",
            "-c",
            sql,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return r.stdout.strip()


def celery_delay(task: str, field_id: str) -> None:
    """Dispatch a Celery task inside the api/processor image via python -c."""
    code = (
        "from app.tasks.soil import fetch_soil_for_field; "
        "from app.tasks.weather import backfill_weather_for_field; "
        f"fid={field_id!r}; "
    )
    if task == "soil":
        code += "print(fetch_soil_for_field.delay(fid).id)"
    elif task == "weather":
        code += "print(backfill_weather_for_field.delay(fid).id)"
    else:
        raise ValueError(task)
    subprocess.run(
        ["docker", "exec", "-i", API_CONTAINER, "python", "-c", code],
        check=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Write geom + enqueue tasks")
    ap.add_argument("--all", action="store_true", help="All agri-tagged fields")
    ap.add_argument("--farm", default=DEFAULT_FARM, help="Farm name filter")
    ap.add_argument("--field-id", action="append", default=[], help="Limit to field UUID(s)")
    args = ap.parse_args()

    where = [
        "f.deleted_at IS NULL",
        "EXISTS (SELECT 1 FROM jsonb_array_elements_text(COALESCE(f.tags_json,'[]'::jsonb)) t WHERE t LIKE 'agri:%')",
    ]
    if args.field_id:
        ids = ",".join(f"'{x}'::uuid" for x in args.field_id)
        where.append(f"f.id IN ({ids})")
    elif not args.all:
        where.append(f"fm.name = '{args.farm.replace(chr(39), chr(39)+chr(39))}'")

    sql = f"""
SELECT f.id::text,
       f.name,
       (
         SELECT substring(t FROM 6)
         FROM jsonb_array_elements_text(COALESCE(f.tags_json,'[]'::jsonb)) t
         WHERE t LIKE 'agri:%'
         LIMIT 1
       ) AS land_id,
       (f.geom IS NULL OR ST_IsEmpty(f.geom)) AS need_geom,
       (SELECT count(*) FROM soil_field_summary s WHERE s.field_id = f.id) AS soil_n,
       (SELECT count(*) FROM weather_daily w WHERE w.field_id = f.id) AS weather_n
FROM fields f
JOIN farms fm ON fm.id = f.farm_id
WHERE {' AND '.join(where)}
ORDER BY f.name;
"""
    out = psql(sql)
    if not out:
        print("No matching agri-tagged fields.")
        return 0

    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        rows.append(
            {
                "id": parts[0],
                "name": parts[1],
                "land_id": parts[2],
                "need_geom": parts[3] in ("t", "true", "1"),
                "soil_n": int(parts[4]),
                "weather_n": int(parts[5]),
            }
        )

    print(f"Found {len(rows)} agri-tagged field(s). apply={args.apply}")
    for r in rows:
        actions = []
        if r["need_geom"]:
            actions.append("sync_geom")
        if r["soil_n"] == 0:
            actions.append("fetch_soil")
        if r["weather_n"] == 0:
            actions.append("backfill_weather")
        print(
            f"  {r['name']} id={r['id']} land={r['land_id']} "
            f"soil={r['soil_n']} weather={r['weather_n']} -> {actions or ['ok']}"
        )
        if not args.apply or not actions:
            continue

        if "sync_geom" in actions and r["land_id"]:
            geom_sql = f"""
UPDATE fields f
SET geom = ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(p.boundary_geojson::text), 4326)),
    area_ha = ROUND((ST_Area(ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(p.boundary_geojson::text), 4326), 6933)) / 10000.0)::numeric, 4),
    updated_at = now()
FROM agri.land_parcels p
WHERE f.id = '{r['id']}'::uuid
  AND p.land_id = '{r['land_id']}'
  AND (f.geom IS NULL OR ST_IsEmpty(f.geom));
"""
            psql(geom_sql)
            print(f"    synced geom from agri.land_parcels {r['land_id']}")

        if "fetch_soil" in actions:
            celery_delay("soil", r["id"])
            print("    enqueued fetch_soil_for_field")
        if "backfill_weather" in actions:
            celery_delay("weather", r["id"])
            print("    enqueued backfill_weather_for_field")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as e:
        print(e.stderr or e.stdout or e, file=sys.stderr)
        raise SystemExit(1)
