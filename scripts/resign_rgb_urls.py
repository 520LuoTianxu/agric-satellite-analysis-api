"""Re-sign agri.parcel_scene_products.rgb_url from rgb_oss_key (20y GET)."""
from __future__ import annotations

import os
from sqlalchemy import create_engine, text
from openfarm_common.storage import get_storage, signed_get_expire_sec

url = (os.environ.get("DATABASE_URL_SYNC") or os.environ["DATABASE_URL"]).replace(
    "postgresql+asyncpg://", "postgresql://"
).replace("postgresql+psycopg://", "postgresql://")
eng = create_engine(url)
st = get_storage()
print("backend", st.backend, "expire", signed_get_expire_sec(years=20))
sample = st.presigned_get("s1s2_parcel/img/6592/2026-09-03_S2/field_rgb.png")
print("sample_ok", ("Signature=" in sample) or ("Expires=" in sample), sample[:140])

updated = 0
with eng.connect() as conn:
    rows = conn.execute(text("""
        SELECT land_id, date::text AS date, scene_id, rgb_oss_key
        FROM agri.parcel_scene_products
        WHERE rgb_oss_key IS NOT NULL AND rgb_oss_key <> ''
          AND sensor = 'S2'
          AND scene_id NOT LIKE '%\\_decloud' ESCAPE '\\'
    """)).mappings().all()
print("todo", len(rows))
with eng.begin() as conn:
    for r in rows:
        key = r["rgb_oss_key"]
        try:
            signed = st.presigned_get(key)
        except Exception as e:
            print("FAIL", r["land_id"], r["date"], e)
            continue
        conn.execute(
            text("""
                UPDATE agri.parcel_scene_products
                SET rgb_url = :u
                WHERE land_id = :lid AND date = :d AND sensor = 'S2'
                  AND scene_id = :sid
            """),
            {"u": signed, "lid": r["land_id"], "d": r["date"], "sid": r["scene_id"]},
        )
        updated += 1
        if updated <= 3 or updated % 50 == 0:
            print("ok", updated, r["land_id"], r["date"])
print("DONE updated", updated)
