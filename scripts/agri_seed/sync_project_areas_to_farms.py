#!/usr/bin/env python3
"""Upsert OpenFarm farms/fields from agri.virtual_project_areas / land_parcels."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

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
    import psycopg

    load_dotenv()
    return psycopg.connect(
        host=os.environ.get("PGHOST", "127.0.0.1"),
        port=os.environ.get("PGPORT", "5432"),
        user=os.environ["PGUSER"],
        password=os.environ.get("PGPASSWORD", ""),
        dbname=os.environ.get("PGDATABASE", "openfarm"),
    )


def pick_name(*parts) -> str:
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


def main() -> int:
    started = time.perf_counter()
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM agri.virtual_project_areas")
            n_tiles = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM agri.land_parcels")
            n_parcels = cur.fetchone()[0]
        print(f"agri tiles={n_tiles} parcels={n_parcels}", flush=True)
        print("using python uuid5 batch upsert", flush=True)

        farm_ins = 0
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tile_id, group_name, county_name, city_name, project_key, province_name
                FROM agri.virtual_project_areas ORDER BY tile_id
                """
            )
            tiles = cur.fetchall()
        batch = []
        for tile_id, group_name, county_name, city_name, project_key, province_name in tiles:
            batch.append(
                (
                    str(farm_id_for(tile_id)),
                    pick_name(group_name, county_name, city_name, project_key, tile_id),
                    (province_name or "").strip() or None,
                    (county_name or "").strip() or None,
                    "Asia/Shanghai",
                )
            )
            if len(batch) >= 500:
                farm_ins += _upsert_farms(conn, batch)
                print(f"[farms] upserted_batch total_attempted={farm_ins}", flush=True)
                batch.clear()
        if batch:
            farm_ins += _upsert_farms(conn, batch)
        print(f"[farms] done attempted={farm_ins}", flush=True)

        field_ins = field_skip = 0
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT land_id, tile_id, land_name, land_area_mu, boundary_geojson::text
                FROM agri.land_parcels ORDER BY land_id
                """
            )
            # stream
            while True:
                rows = cur.fetchmany(200)
                if not rows:
                    break
                payload = []
                for land_id, tile_id, land_name, land_area_mu, boundary_text in rows:
                    area_ha = None
                    if land_area_mu is not None:
                        try:
                            area_ha = float(land_area_mu) / 15.0
                        except Exception:
                            area_ha = None
                    payload.append(
                        (
                            str(field_id_for(land_id)),
                            str(farm_id_for(tile_id)),
                            pick_name(land_name, land_id),
                            boundary_text,
                            area_ha,
                            "unknown",
                            json.dumps([f"agri:{land_id}"]),
                        )
                    )
                ok, skip = _upsert_fields(conn, payload)
                field_ins += ok
                field_skip += skip
                print(
                    f"[fields] inserted~={field_ins} skipped~={field_skip}",
                    flush=True,
                )

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM farms WHERE deleted_at IS NULL")
            nf = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM fields WHERE deleted_at IS NULL")
            nfield = cur.fetchone()[0]
        print(
            f"[done] farms={nf} fields={nfield} elapsed={time.perf_counter()-started:.1f}s",
            flush=True,
        )
    finally:
        conn.close()
    return 0


def _upsert_farms(conn, rows) -> int:
    sql = """
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
    """
    try:
        with conn.cursor() as cur:
            cur.executemany(sql, rows)
        conn.commit()
        return len(rows)
    except Exception as exc:
        conn.rollback()
        print(f"[farms-batch-fallback] {exc}", flush=True)
        n = 0
        for row in rows:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, row)
                conn.commit()
                n += 1
            except Exception as e2:
                conn.rollback()
                print(f"[skip-farm] {e2}", flush=True)
        return n


def _upsert_fields(conn, rows) -> tuple[int, int]:
    sql = """
    INSERT INTO fields (id, farm_id, name, geom, area_ha, crop_type, tags_json)
    VALUES (
      %s, %s, %s,
      ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)),
      %s, %s, %s::jsonb
    )
    ON CONFLICT (id) DO NOTHING
    """
    try:
        with conn.cursor() as cur:
            cur.executemany(sql, rows)
        conn.commit()
        return len(rows), 0
    except Exception as exc:
        conn.rollback()
        print(f"[fields-batch-fallback] {exc}", flush=True)
        ok = skip = 0
        for row in rows:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, row)
                    if cur.rowcount and cur.rowcount > 0:
                        ok += 1
                    else:
                        skip += 1
                conn.commit()
            except Exception as e2:
                conn.rollback()
                skip += 1
                if skip <= 20:
                    print(f"[skip-field] {e2}", flush=True)
        return ok, skip


if __name__ == "__main__":
    raise SystemExit(main())
