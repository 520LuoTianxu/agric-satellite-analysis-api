#!/usr/bin/env python3
"""Bridge STAC/Celery COGs in object storage → agri.parcel_scene_products lonlat_v1.

**Legacy / migration only.** The agri satellite path writes lonlat_v1 in memory
after index compute (``app.tasks.agri_lonlat``) and does not upload index TIFs.
Use this module when you already have ``cogs/{org_id}/{field_id}/{date}/*.tif``
and need a one-shot convert (MQ type ``agri_bridge`` / ``mode=bridge_only``).

Discovers dates under ``cogs/{org_id}/{field_id}/`` on the **configured**
backend (``STORAGE_BACKEND=oss|minio``, default OSS) via ``exists`` probes
(no ListObjects — many OSS bucket policies deny listing), samples the six agri
optical indices (NDVI/EVI/NDMI/NDRE/CIre/MNDWI; NDWI COG only as MNDWI fallback)
inside the field polygon at native COG resolution, and upserts one S2 row per
date with ``pixel_data.format = lonlat_v1``.

COGs are opened via GDAL ``/vsis3/`` using ``app.core.storage.configure_gdal_vsis3``
(same path as index pipeline uploads). Happy path does **not** require MinIO.

Usage (api / processor container)::

    python -c "from app.tasks.bridge_stac_cogs_to_agri_lonlat import main; \
      raise SystemExit(main(['--field-id','0fa5ecc0-944b-4202-a0f8-1ba78ae3746c']))"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import psycopg2
import psycopg2.extras
import rasterio
from rasterio.features import geometry_mask
from rasterio.transform import xy
from rasterio.warp import transform_geom

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Pixel index keys in lonlat_v1.
# Prefer true mndwi.tif; ndwi.tif is only a fallback when mndwi is absent.
PRIMARY_BAND_FILES = (
    ("ndvi", "NDVI"),
    ("evi", "EVI"),
    ("ndmi", "NDMI"),
    ("ndre", "NDRE"),
    ("cire", "CIre"),
    ("mndwi", "MNDWI"))
MNDWI_FALLBACK = ("ndwi", "MNDWI")  # legacy NDWI COG → MNDWI column
REQUIRED_BAND = "ndvi"
# Pixel keys emitted into lonlat_v1 (order stable for UI)
EMIT_PIXEL_KEYS = ("NDVI", "EVI", "NDMI", "NDRE", "CIre", "MNDWI")

UPSERT_SQL = """
INSERT INTO agri.parcel_scene_products (
  land_id, tile_id, date, sensor, scene_id, land_name,
  cloud_cover, cloud_cover_over_30, parcel_cloud_cover_pct,
  json_oss_key, pixel_count, generated_at_shanghai,
  pixel_data_url,
  ndvi_avg, ndvi_min, ndvi_max,
  evi_avg, evi_min, evi_max,
  ndmi_avg, ndmi_min, ndmi_max,
  ndre_avg, ndre_min, ndre_max,
  cire_avg, cire_min, cire_max,
  mndwi_avg, mndwi_min, mndwi_max,
  pixel_data
) VALUES (
  %(land_id)s, %(tile_id)s, %(date)s, 'S2', %(scene_id)s, %(land_name)s,
  %(cloud_cover)s, %(cloud_cover_over_30)s, %(parcel_cloud_cover_pct)s,
  %(json_oss_key)s, %(pixel_count)s, %(generated_at_shanghai)s,
  %(pixel_data_url)s,
  %(ndvi_avg)s, %(ndvi_min)s, %(ndvi_max)s,
  %(evi_avg)s, %(evi_min)s, %(evi_max)s,
  %(ndmi_avg)s, %(ndmi_min)s, %(ndmi_max)s,
  %(ndre_avg)s, %(ndre_min)s, %(ndre_max)s,
  %(cire_avg)s, %(cire_min)s, %(cire_max)s,
  %(mndwi_avg)s, %(mndwi_min)s, %(mndwi_max)s,
  %(pixel_data)s::jsonb
)
ON CONFLICT (land_id, date, sensor, scene_id) DO UPDATE SET
  tile_id = EXCLUDED.tile_id,
  land_name = EXCLUDED.land_name,
  cloud_cover = EXCLUDED.cloud_cover,
  cloud_cover_over_30 = EXCLUDED.cloud_cover_over_30,
  parcel_cloud_cover_pct = EXCLUDED.parcel_cloud_cover_pct,
  json_oss_key = COALESCE(EXCLUDED.json_oss_key, agri.parcel_scene_products.json_oss_key),
  pixel_count = EXCLUDED.pixel_count,
  generated_at_shanghai = EXCLUDED.generated_at_shanghai,
  pixel_data_url = EXCLUDED.pixel_data_url,
  ndvi_avg = EXCLUDED.ndvi_avg,
  ndvi_min = EXCLUDED.ndvi_min,
  ndvi_max = EXCLUDED.ndvi_max,
  evi_avg = EXCLUDED.evi_avg,
  evi_min = EXCLUDED.evi_min,
  evi_max = EXCLUDED.evi_max,
  ndmi_avg = EXCLUDED.ndmi_avg,
  ndmi_min = EXCLUDED.ndmi_min,
  ndmi_max = EXCLUDED.ndmi_max,
  ndre_avg = EXCLUDED.ndre_avg,
  ndre_min = EXCLUDED.ndre_min,
  ndre_max = EXCLUDED.ndre_max,
  cire_avg = EXCLUDED.cire_avg,
  cire_min = EXCLUDED.cire_min,
  cire_max = EXCLUDED.cire_max,
  mndwi_avg = EXCLUDED.mndwi_avg,
  mndwi_min = EXCLUDED.mndwi_min,
  mndwi_max = EXCLUDED.mndwi_max,
  pixel_data = EXCLUDED.pixel_data,
  ingested_at = now()
"""


def _dsn() -> str:
    url = os.environ.get("DATABASE_URL_SYNC") or os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL_SYNC or DATABASE_URL required")
    if url.startswith("postgresql+"):
        _scheme, rest = url.split("://", 1)
        url = "postgresql://" + rest
    return url


def _round6(v: float) -> float:
    return float(round(float(v), 6))


def _stats(arr: np.ndarray) -> tuple[float | None, float | None, float | None]:
    valid = arr[np.isfinite(arr)]
    if valid.size == 0:
        return None, None, None
    return (
        _round6(float(np.mean(valid))),
        _round6(float(np.min(valid))),
        _round6(float(np.max(valid))))


def _load_field(conn, field_id: str, land_id: str | None) -> dict[str, Any]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT f.id::text AS field_id,
                   
                   f.name AS field_name,
                   f.tags_json,
                   ST_AsGeoJSON(f.geom)::text AS geom_geojson
            FROM fields f
            WHERE f.id = %s::uuid AND f.deleted_at IS NULL
            """,
            (field_id,),  # (x,) required; (x) is a str and psycopg2 binds each char
        )
        row = cur.fetchone()
        if not row:
            raise SystemExit(f"field not found: {field_id}")
        if not row["geom_geojson"]:
            raise SystemExit(f"field {field_id} has no geom")

        resolved = land_id
        if not resolved:
            tags = row["tags_json"] or []
            if isinstance(tags, str):
                tags = json.loads(tags)
            for t in tags:
                if isinstance(t, str) and t.startswith("agri:"):
                    resolved = t[5:]
                    break
        if not resolved:
            raise SystemExit("pass --land-id or set agri:<land_id> on field tags")

        cur.execute(
            """
            SELECT land_id, tile_id, land_name
            FROM agri.land_parcels
            WHERE land_id = %s
            """,
            (resolved,),
        )
        parcel = cur.fetchone()
        if not parcel:
            raise SystemExit(f"agri.land_parcels missing land_id={resolved}")

        return {
            "field_id": row["field_id"],

            "field_name": row["field_name"],
            "geom": json.loads(row["geom_geojson"]),
            "land_id": parcel["land_id"],
            "tile_id": parcel["tile_id"],
            "land_name": parcel["land_name"] or row["field_name"],
        }


def _field_stats_map(conn, field_id: str) -> dict[tuple[str, str], dict[str, float]]:
    """(date_iso, LAYER_TYPE) → {mean,min,max,quality_score}."""
    out: dict[tuple[str, str], dict[str, float]] = {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT rl.date::text AS d, rl.layer_type,
                   fs.mean, fs.min, fs.max, fs.quality_score
            FROM field_stats fs
            JOIN raster_layers rl ON rl.id = fs.layer_id
            WHERE fs.field_id = %s::uuid
            """,
            (field_id,),
        )
        for r in cur.fetchall():
            out[(r["d"], r["layer_type"])] = {
                "mean": r["mean"],
                "min": r["min"],
                "max": r["max"],
                "quality_score": r["quality_score"],
            }
    return out


def _list_dates_from_storage(storage, prefix: str) -> list[str]:
    """Best-effort list via storage.list_keys (often denied on OSS).

    Prefer ``_discover_dates_via_exists`` — bucket policies commonly block
    ListObjects while still allowing GetObject/HeadObject.
    """
    dates: set[str] = set()
    try:
        for key in storage.list_keys(prefix, suffix=".tif"):
            parts = key.split("/")
            # cogs/{org}/{field}/{date}/ndvi.tif
            if len(parts) < 5:
                continue
            d = parts[3]
            if DATE_RE.match(d):
                dates.add(d)
    except Exception as e:  # noqa: BLE001
        print(
            f"  storage.list_keys skipped ({type(e).__name__}: {e})",
            file=sys.stderr)
    return sorted(dates)


def _list_dates_from_db(conn, field_id: str) -> list[str]:
    dates: set[str] = set()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT date::text
            FROM raster_layers
            WHERE field_id = %s::uuid AND date IS NOT NULL
            """,
            (field_id,),
        )
        for (d,) in cur.fetchall():
            if d and DATE_RE.match(d):
                dates.add(d)
    return sorted(dates)


def _candidate_dates_from_jobs(conn, field_id: str) -> list[str]:
    """Expand job params date_from/date_to into daily candidates (inclusive)."""
    from datetime import date, timedelta

    ranges: list[tuple[date, date]] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT
              params_json->>'date_from' AS df,
              params_json->>'date_to' AS dt
            FROM jobs
            WHERE field_id = %s::uuid
              AND params_json ? 'date_from'
              AND params_json ? 'date_to'
            """,
            (field_id,),
        )
        for df, dt in cur.fetchall():
            if not df or not dt:
                continue
            try:
                start = date.fromisoformat(str(df)[:10])
                end = date.fromisoformat(str(dt)[:10])
            except ValueError:
                continue
            if end < start:
                start, end = end, start
            ranges.append((start, end))
    if not ranges:
        # Fallback: last ~24 months ending today (Shanghai)
        end = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        start = (
            end.replace(year=end.year - 2)
            if end.month != 2 or end.day != 29
            else end.replace(year=end.year - 2, day=28)
        )
        ranges.append((start, end))

    out: set[str] = set()
    for start, end in ranges:
        cur_d = start
        # Cap runaway ranges
        for _ in range(900):
            out.add(cur_d.isoformat())
            if cur_d >= end:
                break
            cur_d += timedelta(days=1)
    return sorted(out)


def _discover_dates_via_exists(
    storage,
    prefix: str,
    candidates: list[str],
    *,
    stems: tuple[str, ...] = (REQUIRED_BAND, "vv", "vh")) -> list[str]:
    """Probe ``prefix{date}/{stem}.tif`` with Head/exists — no ListObjects."""
    found: set[str] = set()
    for d in candidates:
        if not DATE_RE.match(d):
            continue
        for stem in stems:
            key = f"{prefix}{d}/{stem}.tif"
            try:
                if storage.exists(key):
                    found.add(d)
                    break
            except Exception as e:  # noqa: BLE001
                print(
                    f"  exists({key}) failed ({type(e).__name__}: {e})",
                    file=sys.stderr)
                break
    return sorted(found)


def _vsis3(bucket: str, key: str) -> str:
    return f"/vsis3/{bucket}/{key}"


def _open_band(path: str) -> tuple[np.ndarray, Any, Any] | None:
    try:
        ds = rasterio.open(path)
    except Exception as e:  # noqa: BLE001
        print(f"  skip unreadable {path}: {e}", file=sys.stderr)
        return None
    try:
        data = ds.read(1).astype(np.float32)
        return data, ds.transform, ds.crs
    finally:
        ds.close()


def _sample_lonlat(
    geom4326: dict,
    bands: dict[str, np.ndarray],
    transform,
    crs,
    *,
    scl: np.ndarray | None = None,
) -> list[dict[str, Any]]:
    """Emit lonlat_v1 pixels for cells inside polygon with finite NDVI."""
    from app.core.agri_classify import is_scl_cloudy_class

    if "NDVI" not in bands:
        return []
    ndvi = bands["NDVI"]
    h, w = ndvi.shape

    geom = geom4326
    if crs and str(crs) not in ("EPSG:4326", "OGC:CRS84"):
        geom = transform_geom("EPSG:4326", crs, geom4326)

    inside = ~geometry_mask(
        [geom],
        out_shape=(h, w),
        transform=transform,
        all_touched=False,
        invert=False)
    finite = np.isfinite(ndvi) & inside
    rows, cols = np.where(finite)
    if rows.size == 0:
        rows, cols = np.where(np.isfinite(ndvi))
    if rows.size == 0:
        return []

    xs, ys = xy(transform, rows, cols, offset="center")
    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)

    if crs and str(crs) not in ("EPSG:4326", "OGC:CRS84"):
        from rasterio.warp import transform as warp_xy

        lons, lats = warp_xy(crs, "EPSG:4326", xs.tolist(), ys.tolist())
        xs = np.asarray(lons, dtype=np.float64)
        ys = np.asarray(lats, dtype=np.float64)

    scl_ok = (
        scl is not None
        and isinstance(scl, np.ndarray)
        and scl.shape == ndvi.shape
    )
    pixels: list[dict[str, Any]] = []
    emit_keys = [k for k in EMIT_PIXEL_KEYS if k in bands]
    for i in range(rows.size):
        r, c = int(rows[i]), int(cols[i])
        clear = 1
        if scl_ok:
            sv = scl[r, c]
            if np.isfinite(sv) and is_scl_cloudy_class(sv):
                clear = 0
        pix: dict[str, Any] = {
            "lon": _round6(xs[i]),
            "lat": _round6(ys[i]),
            "clear": clear,
        }
        ok = True
        for key in emit_keys:
            v = bands[key][r, c]
            if not np.isfinite(v):
                if key == "NDVI":
                    ok = False
                    break
                continue
            pix[key] = _round6(v)
        if ok and "NDVI" in pix:
            pixels.append(pix)
    return pixels


def _pick_stats(
    sampled: tuple[float | None, float | None, float | None],
    fs: dict[str, float] | None) -> tuple[float | None, float | None, float | None]:
    if fs and fs.get("mean") is not None:
        return (
            _round6(fs["mean"]) if fs.get("mean") is not None else None,
            _round6(fs["min"]) if fs.get("min") is not None else None,
            _round6(fs["max"]) if fs.get("max") is not None else None)
    return sampled


def process_date(
    *,
    bucket: str,
    prefix: str,
    date_str: str,
    meta: dict[str, Any],
    fs_map: dict[tuple[str, str], dict[str, float]],
    dry_run: bool,
    storage=None) -> dict[str, Any] | None:
    band_arrays: dict[str, np.ndarray] = {}
    transform = None
    crs = None
    loaded_stems: set[str] = set()

    def _load_one(file_stem: str, pix_key: str, *, required: bool) -> bool:
        nonlocal transform, crs
        key = f"{prefix}{date_str}/{file_stem}.tif"
        # Prefer existence check on configured backend when available
        if storage is not None:
            try:
                if not storage.exists(key):
                    if required:
                        print(
                            f"  skip {date_str}: no {file_stem}.tif on {storage.backend}",
                            file=sys.stderr)
                    return False
            except Exception:  # noqa: BLE001
                pass
        path = _vsis3(bucket, key)
        opened = _open_band(path)
        if opened is None:
            if required:
                print(
                    f"  skip {date_str}: no readable NDVI on active store",
                    file=sys.stderr)
            return False
        data, t, c = opened
        if required:
            transform, crs = t, c
            band_arrays[pix_key] = data
            loaded_stems.add(file_stem)
            return True
        if "NDVI" not in band_arrays or data.shape != band_arrays["NDVI"].shape:
            print(
                f"  warn {date_str} {file_stem}: shape mismatch, omit",
                file=sys.stderr)
            return False
        if pix_key in band_arrays:
            return False
        band_arrays[pix_key] = data
        loaded_stems.add(file_stem)
        return True

    if not _load_one(REQUIRED_BAND, "NDVI", required=True):
        return None

    for file_stem, pix_key in PRIMARY_BAND_FILES:
        if file_stem == REQUIRED_BAND:
            continue
        _load_one(file_stem, pix_key, required=False)

    if "MNDWI" not in band_arrays:
        _load_one(MNDWI_FALLBACK[0], MNDWI_FALLBACK[1], required=False)

    assert transform is not None
    pixels = _sample_lonlat(meta["geom"], band_arrays, transform, crs)
    if not pixels:
        print(f"  skip {date_str}: 0 pixels inside polygon", file=sys.stderr)
        return None

    def _avg_triple(pix_key: str, layer_label: str):
        sampled = (
            _stats(band_arrays[pix_key])
            if pix_key in band_arrays
            else (None, None, None)
        )
        return _pick_stats(sampled, fs_map.get((date_str, layer_label)))

    ndvi_avg, ndvi_min, ndvi_max = _avg_triple("NDVI", "NDVI")
    evi_avg, evi_min, evi_max = _avg_triple("EVI", "EVI")
    ndmi_avg, ndmi_min, ndmi_max = _avg_triple("NDMI", "NDMI")
    ndre_avg, ndre_min, ndre_max = _avg_triple("NDRE", "NDRE")
    cire_avg, cire_min, cire_max = _avg_triple("CIre", "CIRE")
    mndwi_s = (
        _stats(band_arrays["MNDWI"]) if "MNDWI" in band_arrays else (None, None, None)
    )
    mndwi_fs = fs_map.get((date_str, "MNDWI")) or fs_map.get((date_str, "NDWI"))
    mndwi_avg, mndwi_min, mndwi_max = _pick_stats(mndwi_s, mndwi_fs)

    # Do not derive parcel cloud from field_stats quality_score (that is
    # window fill, not cloud). Without SCL, leave parcel_cloud None so
    # callers fall back to STAC cloud_cover.
    cloud_over_30 = False
    parcel_cloud = None

    pixel_data = {"format": "lonlat_v1", "pixels": pixels}
    scene_id = f"stac_bridge_{date_str}_S2"
    row = {
        "land_id": meta["land_id"],
        "tile_id": meta["tile_id"],
        "date": date_str,
        "scene_id": scene_id,
        "land_name": meta["land_name"],
        "cloud_cover": None,
        "cloud_cover_over_30": cloud_over_30,
        "parcel_cloud_cover_pct": parcel_cloud,
        "pixel_count": len(pixels),
        "generated_at_shanghai": datetime.now(ZoneInfo("Asia/Shanghai")).strftime(
            "%Y-%m-%d %H:%M:%S%z"
        ),
        "pixel_data_url": f"stac-bridge://field/{meta['field_id']}/{date_str}",
        "ndvi_avg": ndvi_avg,
        "ndvi_min": ndvi_min,
        "ndvi_max": ndvi_max,
        "evi_avg": evi_avg,
        "evi_min": evi_min,
        "evi_max": evi_max,
        "ndmi_avg": ndmi_avg,
        "ndmi_min": ndmi_min,
        "ndmi_max": ndmi_max,
        "ndre_avg": ndre_avg,
        "ndre_min": ndre_min,
        "ndre_max": ndre_max,
        "cire_avg": cire_avg,
        "cire_min": cire_min,
        "cire_max": cire_max,
        "mndwi_avg": mndwi_avg,
        "mndwi_min": mndwi_min,
        "mndwi_max": mndwi_max,
        "pixel_data": json.dumps(pixel_data, separators=(",", ":")),
        "json_oss_key": None,
        "_pixel_data_obj": pixel_data,
    }
    if dry_run:
        print(
            f"  dry-run {date_str}: pixels={len(pixels)} "
            f"ndvi={ndvi_avg} evi={evi_avg} ndmi={ndmi_avg} "
            f"ndre={ndre_avg} cire={cire_avg} mndwi={mndwi_avg} "
            f"stems={sorted(loaded_stems)}"
        )
    return row


def bridge_field_stac_to_agri(
    field_id: str,
    land_id: str | None = None,
    *,
    dry_run: bool = False,
    limit: int = 0,
    dates: list[str] | None = None,
    quiet: bool = False) -> dict[str, Any]:
    """Sample active-store STAC COGs for *field_id* and upsert agri lonlat_v1 rows.

    Uses ``get_storage()`` (OSS by default). Raises on hard errors.
    """
    from app.core.storage import configure_gdal_vsis3, get_storage

    storage = get_storage()
    configure_gdal_vsis3(storage)
    bucket = storage.bucket
    conn = psycopg2.connect(_dsn())
    conn.autocommit = False

    def _log(msg: str) -> None:
        if not quiet:
            print(msg, flush=True)

    try:
        meta = _load_field(conn, field_id, land_id)
        fs_map = _field_stats_map(conn, meta["field_id"])
        prefix = f"cogs/default/{meta['field_id']}/"
        uri_scheme = "oss" if storage.backend == "oss" else "s3"
        _log(
            f"field={meta['field_id']} land={meta['land_id']} "
            f"tile={meta['tile_id']} backend={storage.backend} "
            f"prefix={uri_scheme}://{bucket}/{prefix}"
        )

        # Prefer exists-probes (OSS often denies ListObjects). list_keys is
        # opportunistic only; job date ranges supply candidate calendars.
        date_set = set(_list_dates_from_db(conn, meta["field_id"]))
        listed = _list_dates_from_storage(storage, prefix)
        if listed:
            date_set.update(listed)
        candidates = _candidate_dates_from_jobs(conn, meta["field_id"])
        probed = _discover_dates_via_exists(storage, prefix, candidates)
        date_set.update(probed)
        date_list = sorted(date_set)
        if dates:
            want = {d.strip() for d in dates if d and d.strip()}
            date_list = [d for d in date_list if d in want]
        if limit and limit > 0:
            date_list = date_list[:limit]
        _log(f"dates_found={len(date_list)} backend={storage.backend}")

        upserted = 0
        skipped = 0
        oss_urls: dict[str, str] = {}
        oss_keys: list[str] = []
        with conn.cursor() as cur:
            for d in date_list:
                row = process_date(
                    bucket=bucket,
                    prefix=prefix,
                    date_str=d,
                    meta=meta,
                    fs_map=fs_map,
                    dry_run=dry_run,
                    storage=storage)
                if row is None:
                    skipped += 1
                    continue
                pixel_obj = row.pop("_pixel_data_obj", None)
                oss_url = None
                if not dry_run:
                    try:
                        from openfarm_common.mq_results import (
                            scene_json_oss_key,
                            upload_scene_product_json)

                        key = scene_json_oss_key(row["land_id"], row["date"], "S2")
                        product = {
                            "land_id": row["land_id"],
                            "tile_id": row["tile_id"],
                            "date": row["date"],
                            "sensor": "S2",
                            "scene_id": row["scene_id"],
                            "land_name": row["land_name"],
                            "cloud_cover": row["cloud_cover"],
                            "cloud_cover_over_30": row["cloud_cover_over_30"],
                            "parcel_cloud_cover_pct": row["parcel_cloud_cover_pct"],
                            "pixel_count": row["pixel_count"],
                            "generated_at_shanghai": row["generated_at_shanghai"],
                            "pixel_data_url": row["pixel_data_url"],
                            "json_oss_key": key,
                            "ndvi_avg": row["ndvi_avg"],
                            "ndvi_min": row["ndvi_min"],
                            "ndvi_max": row["ndvi_max"],
                            "evi_avg": row["evi_avg"],
                            "evi_min": row["evi_min"],
                            "evi_max": row["evi_max"],
                            "ndmi_avg": row["ndmi_avg"],
                            "ndmi_min": row["ndmi_min"],
                            "ndmi_max": row["ndmi_max"],
                            "ndre_avg": row["ndre_avg"],
                            "ndre_min": row["ndre_min"],
                            "ndre_max": row["ndre_max"],
                            "cire_avg": row["cire_avg"],
                            "cire_min": row["cire_min"],
                            "cire_max": row["cire_max"],
                            "mndwi_avg": row["mndwi_avg"],
                            "mndwi_min": row["mndwi_min"],
                            "mndwi_max": row["mndwi_max"],
                            "pixel_data": pixel_obj or json.loads(row["pixel_data"]),
                        }
                        key, oss_url = upload_scene_product_json(
                            land_id=row["land_id"],
                            date_str=row["date"],
                            sensor="S2",
                            product=product)
                        row["json_oss_key"] = key
                        product["json_url"] = oss_url
                    except Exception as exc:  # noqa: BLE001
                        print(
                            f"  warn {d}: scene JSON OSS upload failed: {exc}",
                            file=sys.stderr)
                    cur.execute(UPSERT_SQL, row)
                upserted += 1
                if row.get("json_oss_key") and oss_url:
                    oss_urls[f"{d}_S2"] = oss_url
                elif row.get("json_oss_key"):
                    oss_keys.append(row["json_oss_key"])
                _log(
                    f"  upserted {d} pixels={row['pixel_count']} ndvi={row['ndvi_avg']}"
                    + (
                        f" json={row.get('json_oss_key')}"
                        if row.get("json_oss_key")
                        else ""
                    )
                )
            if not dry_run:
                conn.commit()

        result = {
            "ok": True,
            "land_id": meta["land_id"],
            "field_id": meta["field_id"],
            "backend": storage.backend,
            "bucket": bucket,
            "dates_seen": len(date_list),
            "upserted": upserted,
            "skipped": skipped,
            "dry_run": dry_run,
            "oss_urls": oss_urls,
            "oss_keys": oss_keys,
        }
        _log(json.dumps(result))
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--field-id", required=True, help="public.fields UUID")
    ap.add_argument("--land-id", default=None, help="agri land_id (or agri: tag)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="Max dates (0=all)")
    ap.add_argument(
        "--dates",
        default="",
        help="Comma-separated YYYY-MM-DD subset (optional)")
    args = ap.parse_args(argv)
    dates = [d.strip() for d in args.dates.split(",") if d.strip()] or None
    bridge_field_stac_to_agri(
        args.field_id,
        args.land_id,
        dry_run=args.dry_run,
        limit=args.limit,
        dates=dates)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
