#!/usr/bin/env python3
"""Backfill parcel true-color RGB for land_id=4933 (2025-08..10 raw S2).

Uploads field_rgb.png to OSS, patches scene JSON, UPDATEs agri.parcel_scene_products
rgb_url / rgb_oss_key. Safe to re-run.
"""
from __future__ import annotations

import io
import json
import os
import sys
from datetime import date

import numpy as np
from PIL import Image
from pystac_client import Client
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from sqlalchemy import create_engine, text

# Prefer installed app helpers when running inside ingest container.
sys.path.insert(0, "/app")
try:
    from app.tasks.pipeline import compute_target_grid, read_band_windowed
except Exception:
    from app.tasks.pipeline import compute_target_grid, read_band_windowed  # type: ignore

from openfarm_common.settings import settings
from openfarm_common.storage import get_storage

LAND_ID = "4933"
DATE_FROM = date(2025, 8, 1)
DATE_TO = date(2025, 10, 31)
STAC_API = os.environ.get("STAC_API_URL", "https://earth-search.aws.element84.com/v1")
COLLECTION = os.environ.get("STAC_COLLECTION", "sentinel-2-l2a")


def db_url() -> str:
    url = os.environ.get("DATABASE_URL_SYNC") or os.environ["DATABASE_URL"]
    return (
        url.replace("postgresql+asyncpg://", "postgresql://").replace(
            "postgresql+psycopg://", "postgresql://"
        )
    )


def field_rgb_oss_key(land_id: str, date_str: str) -> str:
    json_prefix = (settings.oss_prefix or "s1s2_parcel/json/").rstrip("/")
    if json_prefix.endswith("/json"):
        img_root = json_prefix[: -len("/json")] + "/img"
    else:
        img_root = "s1s2_parcel/img"
    return f"{img_root}/{land_id}/{date_str}_S2/field_rgb.png"


def stretch(band: np.ndarray, mask: np.ndarray) -> np.ndarray:
    out = np.zeros(band.shape, dtype=np.uint8)
    valid = mask & np.isfinite(band)
    if not np.any(valid):
        return out
    vals = band[valid].astype(np.float64)
    lo, hi = np.percentile(vals, [2, 98])
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        lo = float(np.nanmin(vals))
        hi = float(np.nanmax(vals))
        if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
            return out
    scaled = np.clip((band.astype(np.float64) - lo) / (hi - lo), 0, 1)
    out[valid] = (scaled[valid] * 255).astype(np.uint8)
    return out


def render_png(b02, b03, b04, mask) -> bytes:
    r, g, b = stretch(b04, mask), stretch(b03, mask), stretch(b02, mask)
    a = np.where(mask, 255, 0).astype(np.uint8)
    img = Image.fromarray(np.dstack([r, g, b, a]), mode="RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def asset_href(item, *names: str) -> str | None:
    for n in names:
        a = item.assets.get(n)
        if a and a.href:
            return a.href
    return None


def main() -> int:
    eng = create_engine(db_url())
    storage = get_storage()
    with eng.connect() as conn:
        field = conn.execute(
            text(
                """
                SELECT id::text, name, ST_AsGeoJSON(geom)::json AS geom
                FROM fields
                WHERE tags_json::text LIKE :pat OR name = :name
                LIMIT 1
                """
            ),
            {"pat": f"%agri:{LAND_ID}%", "name": "贾河北村15号地块"},
        ).mappings().first()
        if not field:
            print("FIELD_NOT_FOUND")
            return 1
        rows = conn.execute(
            text(
                """
                SELECT date::text AS date, scene_id, json_oss_key
                FROM agri.parcel_scene_products
                WHERE land_id = :lid AND sensor = 'S2'
                  AND date >= :d0 AND date <= :d1
                  AND scene_id NOT LIKE '%\\_decloud' ESCAPE '\\'
                ORDER BY date
                """
            ),
            {"lid": LAND_ID, "d0": DATE_FROM, "d1": DATE_TO},
        ).mappings().all()

    geom = field["geom"]
    print(f"field={field['id']} rows={len(rows)}")
    catalog = Client.open(STAC_API)
    ok = 0
    fail = 0
    for row in rows:
        d = row["date"]
        key_json = row["json_oss_key"]
        try:
            items = list(
                catalog.search(
                    collections=[COLLECTION],
                    intersects=geom,
                    datetime=f"{d}T00:00:00Z/{d}T23:59:59Z",
                    max_items=5,
                ).items()
            )
            if not items:
                print(f"SKIP {d} no STAC")
                fail += 1
                continue
            # prefer lowest cloud
            items.sort(key=lambda it: float(it.properties.get("eo:cloud_cover") or 999))
            item = items[0]
            href_b02 = asset_href(item, "blue", "B02")
            href_b03 = asset_href(item, "green", "B03")
            href_b04 = asset_href(item, "red", "B04")
            if not (href_b02 and href_b03 and href_b04):
                print(f"SKIP {d} missing RGB bands")
                fail += 1
                continue

            # build grid from red band
            from shapely.geometry import shape

            geom_shp = shape(geom)
            bounds = geom_shp.bounds
            # reuse pipeline grid: need field_geom geojson
            target_transform, target_shape, field_mask, _bounds = compute_target_grid(
                bounds, geom
            )
            b02 = read_band_windowed(
                href_b02, bounds, target_shape, target_transform
            )
            b03 = read_band_windowed(
                href_b03, bounds, target_shape, target_transform
            )
            b04 = read_band_windowed(
                href_b04, bounds, target_shape, target_transform
            )
            if b02 is None or b03 is None or b04 is None:
                print(f"SKIP {d} band read failed")
                fail += 1
                continue
            png = render_png(b02, b03, b04, field_mask.astype(bool))
            img_key = field_rgb_oss_key(LAND_ID, d)
            storage.put_bytes(img_key, png, content_type="image/png")
            img_url = storage.presigned_get(img_key)

            # patch OSS JSON
            if key_json:
                try:
                    raw = storage.get_bytes(key_json)
                    obj = json.loads(raw)
                    if isinstance(obj, dict):
                        obj["rgb_url"] = img_url
                        obj["rgb_oss_key"] = img_key
                        storage.put_bytes(
                            key_json,
                            json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode(),
                            content_type="application/json",
                        )
                except Exception as exc:
                    print(f"WARN {d} json patch failed: {exc}")

            with eng.begin() as conn:
                conn.execute(
                    text(
                        """
                        UPDATE agri.parcel_scene_products
                        SET rgb_url = :u, rgb_oss_key = :k
                        WHERE land_id = :lid AND date = :d AND sensor = 'S2'
                          AND scene_id NOT LIKE '%\\_decloud' ESCAPE '\\'
                        """
                    ),
                    {"u": img_url, "k": img_key, "lid": LAND_ID, "d": d},
                )
            print(f"OK {d} {img_key}")
            ok += 1
        except Exception as exc:
            print(f"FAIL {d} {exc}")
            fail += 1
    print(f"DONE ok={ok} fail={fail}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
