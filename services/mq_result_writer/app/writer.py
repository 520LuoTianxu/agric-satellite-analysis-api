"""Persist ResultMessage payloads into agri.mq_task_results (+ domain upserts)."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from openfarm_common.database_sync import SyncSession
from openfarm_common.mq_schemas import ResultMessage
from openfarm_common.settings import settings
from openfarm_common.storage import get_storage
from sqlalchemy import text

logger = logging.getLogger(__name__)

UPSERT_WEATHER_SQL = """
INSERT INTO weather_daily (
  id, field_id, date, latitude, longitude,
  temperature_2m_min, temperature_2m_max, temperature_2m_mean,
  precipitation_sum, et0_fao_mm,
  soil_temperature_0cm, soil_temperature_6cm,
  soil_temperature_18cm, soil_temperature_54cm,
  soil_moisture_0_1cm, soil_moisture_1_3cm, soil_moisture_3_9cm,
  soil_moisture_9_27cm, soil_moisture_27_81cm,
  vapor_pressure_deficit, shortwave_radiation_sum,
  wind_speed_10m_max, cloud_cover_mean,
  gdd_daily, gdd_cumulative, water_balance_30d_mm, drought_index,
  heat_stress_flag, source, model_used, updated_at
) VALUES (
  :id, CAST(:field_id AS uuid), CAST(:date AS date),
  :latitude, :longitude,
  :temperature_2m_min, :temperature_2m_max, :temperature_2m_mean,
  :precipitation_sum, :et0_fao_mm,
  :soil_temperature_0cm, :soil_temperature_6cm,
  :soil_temperature_18cm, :soil_temperature_54cm,
  :soil_moisture_0_1cm, :soil_moisture_1_3cm, :soil_moisture_3_9cm,
  :soil_moisture_9_27cm, :soil_moisture_27_81cm,
  :vapor_pressure_deficit, :shortwave_radiation_sum,
  :wind_speed_10m_max, :cloud_cover_mean,
  :gdd_daily, :gdd_cumulative, :water_balance_30d_mm, :drought_index,
  :heat_stress_flag, :source, :model_used, now()
)
ON CONFLICT (field_id, date) DO UPDATE SET
  latitude = EXCLUDED.latitude,
  longitude = EXCLUDED.longitude,
  temperature_2m_min = EXCLUDED.temperature_2m_min,
  temperature_2m_max = EXCLUDED.temperature_2m_max,
  temperature_2m_mean = EXCLUDED.temperature_2m_mean,
  precipitation_sum = EXCLUDED.precipitation_sum,
  et0_fao_mm = EXCLUDED.et0_fao_mm,
  soil_temperature_0cm = EXCLUDED.soil_temperature_0cm,
  soil_temperature_6cm = EXCLUDED.soil_temperature_6cm,
  soil_temperature_18cm = EXCLUDED.soil_temperature_18cm,
  soil_temperature_54cm = EXCLUDED.soil_temperature_54cm,
  soil_moisture_0_1cm = EXCLUDED.soil_moisture_0_1cm,
  soil_moisture_1_3cm = EXCLUDED.soil_moisture_1_3cm,
  soil_moisture_3_9cm = EXCLUDED.soil_moisture_3_9cm,
  soil_moisture_9_27cm = EXCLUDED.soil_moisture_9_27cm,
  soil_moisture_27_81cm = EXCLUDED.soil_moisture_27_81cm,
  vapor_pressure_deficit = EXCLUDED.vapor_pressure_deficit,
  shortwave_radiation_sum = EXCLUDED.shortwave_radiation_sum,
  wind_speed_10m_max = EXCLUDED.wind_speed_10m_max,
  cloud_cover_mean = EXCLUDED.cloud_cover_mean,
  gdd_daily = EXCLUDED.gdd_daily,
  gdd_cumulative = EXCLUDED.gdd_cumulative,
  water_balance_30d_mm = EXCLUDED.water_balance_30d_mm,
  drought_index = EXCLUDED.drought_index,
  heat_stress_flag = EXCLUDED.heat_stress_flag,
  source = EXCLUDED.source,
  model_used = EXCLUDED.model_used,
  updated_at = now()
"""

UPSERT_SCENE_SQL = """
INSERT INTO agri.parcel_scene_products (
  land_id, tile_id, date, sensor, scene_id, land_name,
  cloud_cover, cloud_cover_over_30, parcel_cloud_cover_pct,
  json_oss_key, pixel_count, generated_at_shanghai,
  pixel_data_url,
  rgb_url, large_rgb_url, rgb_oss_key,
  ndvi_avg, ndvi_min, ndvi_max,
  evi_avg, evi_min, evi_max,
  ndmi_avg, ndmi_min, ndmi_max,
  ndre_avg, ndre_min, ndre_max,
  cire_avg, cire_min, cire_max,
  mndwi_avg, mndwi_min, mndwi_max,
  vv_avg, vv_min, vv_max,
  vh_avg, vh_min, vh_max,
  pixel_data
) VALUES (
  %(land_id)s, %(tile_id)s, %(date)s, %(sensor)s, %(scene_id)s, %(land_name)s,
  %(cloud_cover)s, %(cloud_cover_over_30)s, %(parcel_cloud_cover_pct)s,
  %(json_oss_key)s, %(pixel_count)s, %(generated_at_shanghai)s,
  %(pixel_data_url)s,
  %(rgb_url)s, %(large_rgb_url)s, %(rgb_oss_key)s,
  %(ndvi_avg)s, %(ndvi_min)s, %(ndvi_max)s,
  %(evi_avg)s, %(evi_min)s, %(evi_max)s,
  %(ndmi_avg)s, %(ndmi_min)s, %(ndmi_max)s,
  %(ndre_avg)s, %(ndre_min)s, %(ndre_max)s,
  %(cire_avg)s, %(cire_min)s, %(cire_max)s,
  %(mndwi_avg)s, %(mndwi_min)s, %(mndwi_max)s,
  %(vv_avg)s, %(vv_min)s, %(vv_max)s,
  %(vh_avg)s, %(vh_min)s, %(vh_max)s,
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
  rgb_url = COALESCE(EXCLUDED.rgb_url, agri.parcel_scene_products.rgb_url),
  large_rgb_url = COALESCE(EXCLUDED.large_rgb_url, agri.parcel_scene_products.large_rgb_url),
  rgb_oss_key = COALESCE(EXCLUDED.rgb_oss_key, agri.parcel_scene_products.rgb_oss_key),
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
  vv_avg = COALESCE(EXCLUDED.vv_avg, agri.parcel_scene_products.vv_avg),
  vv_min = COALESCE(EXCLUDED.vv_min, agri.parcel_scene_products.vv_min),
  vv_max = COALESCE(EXCLUDED.vv_max, agri.parcel_scene_products.vv_max),
  vh_avg = COALESCE(EXCLUDED.vh_avg, agri.parcel_scene_products.vh_avg),
  vh_min = COALESCE(EXCLUDED.vh_min, agri.parcel_scene_products.vh_min),
  vh_max = COALESCE(EXCLUDED.vh_max, agri.parcel_scene_products.vh_max),
  pixel_data = EXCLUDED.pixel_data,
  ingested_at = now()
"""


def _download_json(url: str) -> Any | None:
    """HTTP GET JSON from a public OSS URL, or storage.get for our bucket keys."""
    try:
        parsed = urlparse(url)
        bucket = settings.oss_bucket
        if bucket and parsed.netloc.startswith(f"{bucket}."):
            key = parsed.path.lstrip("/")
            if key:
                raw = get_storage().get_bytes(key)
                return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        logger.warning("storage_get_failed url=%s err=%s", url[:120], exc)

    try:
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.warning("http_get_failed url=%s err=%s", url[:120], exc)
        return None


def upsert_mq_task_result(msg: ResultMessage, payloads: dict[str, Any]) -> None:
    session = SyncSession()
    try:
        # Prefer storing the resolved domain payload (inline + downloads).
        stored = dict(payloads)
        if msg.payload and "inline" not in stored:
            stored["inline"] = msg.payload
        session.execute(
            text(
                """
                INSERT INTO agri.mq_task_results (
                    task_id, status, oss_urls, payload, error,
                    field_id, land_id, finished_at, updated_at
                ) VALUES (
                    :task_id, :status, CAST(:oss_urls AS jsonb), CAST(:payload AS jsonb),
                    :error, :field_id, :land_id, :finished_at, now()
                )
                ON CONFLICT (task_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    oss_urls = EXCLUDED.oss_urls,
                    payload = EXCLUDED.payload,
                    error = EXCLUDED.error,
                    field_id = COALESCE(EXCLUDED.field_id, agri.mq_task_results.field_id),
                    land_id = COALESCE(EXCLUDED.land_id, agri.mq_task_results.land_id),
                    finished_at = EXCLUDED.finished_at,
                    updated_at = now()
                """
            ),
            {
                "task_id": msg.task_id,
                "status": msg.status,
                "oss_urls": json.dumps(msg.oss_urls, ensure_ascii=False),
                "payload": json.dumps(stored, default=str, ensure_ascii=False),
                "error": msg.error,
                "field_id": msg.field_id,
                "land_id": msg.land_id,
                "finished_at": msg.finished_at,
            },
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _is_lonlat_scene_product(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    pixel_data = obj.get("pixel_data")
    if isinstance(pixel_data, str):
        try:
            pixel_data = json.loads(pixel_data)
        except Exception:
            return False
    if not isinstance(pixel_data, dict):
        return False
    if pixel_data.get("format") != "lonlat_v1":
        return False
    return bool(obj.get("land_id") and obj.get("date") and obj.get("scene_id"))


def apply_parcel_scene_product(
    obj: dict[str, Any], *, json_oss_key: str | None = None
) -> None:
    """Upsert one lonlat_v1 scene product into agri.parcel_scene_products."""
    pixel_data = obj.get("pixel_data")
    if isinstance(pixel_data, dict):
        pixel_data_str = json.dumps(pixel_data, separators=(",", ":"))
    else:
        pixel_data_str = pixel_data
    sensor = obj.get("sensor") or "S2"
    params = {
        "land_id": obj["land_id"],
        "tile_id": obj.get("tile_id") or "",
        "date": obj["date"],
        "sensor": sensor,
        "scene_id": obj["scene_id"],
        "land_name": obj.get("land_name"),
        "cloud_cover": obj.get("cloud_cover"),
        "cloud_cover_over_30": obj.get("cloud_cover_over_30"),
        "parcel_cloud_cover_pct": obj.get("parcel_cloud_cover_pct"),
        "json_oss_key": json_oss_key or obj.get("json_oss_key"),
        "pixel_count": obj.get("pixel_count"),
        "generated_at_shanghai": obj.get("generated_at_shanghai"),
        "pixel_data_url": obj.get("pixel_data_url"),
        "rgb_url": obj.get("rgb_url"),
        "large_rgb_url": obj.get("large_rgb_url"),
        "rgb_oss_key": obj.get("rgb_oss_key"),
        "ndvi_avg": obj.get("ndvi_avg"),
        "ndvi_min": obj.get("ndvi_min"),
        "ndvi_max": obj.get("ndvi_max"),
        "evi_avg": obj.get("evi_avg"),
        "evi_min": obj.get("evi_min"),
        "evi_max": obj.get("evi_max"),
        "ndmi_avg": obj.get("ndmi_avg"),
        "ndmi_min": obj.get("ndmi_min"),
        "ndmi_max": obj.get("ndmi_max"),
        "ndre_avg": obj.get("ndre_avg"),
        "ndre_min": obj.get("ndre_min"),
        "ndre_max": obj.get("ndre_max"),
        "cire_avg": obj.get("cire_avg"),
        "cire_min": obj.get("cire_min"),
        "cire_max": obj.get("cire_max"),
        "mndwi_avg": obj.get("mndwi_avg"),
        "mndwi_min": obj.get("mndwi_min"),
        "mndwi_max": obj.get("mndwi_max"),
        "vv_avg": obj.get("vv_avg"),
        "vv_min": obj.get("vv_min"),
        "vv_max": obj.get("vv_max"),
        "vh_avg": obj.get("vh_avg"),
        "vh_min": obj.get("vh_min"),
        "vh_max": obj.get("vh_max"),
        "pixel_data": pixel_data_str,
    }
    session = SyncSession()
    try:
        # SyncSession uses SQLAlchemy; for %-format params use connection.exec_driver_sql
        conn = session.connection()
        conn.exec_driver_sql(UPSERT_SCENE_SQL, params)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def apply_weather_payload(payload: dict[str, Any]) -> int:
    rows = payload.get("rows") or []
    if not isinstance(rows, list) or not rows:
        return 0
    session = SyncSession()
    n = 0
    try:
        for row in rows:
            if not isinstance(row, dict):
                continue
            params = {
                "id": str(uuid.uuid4()),
                "field_id": row.get("field_id") or payload.get("field_id"),
                "date": row.get("date"),
                "latitude": row.get("latitude"),
                "longitude": row.get("longitude"),
                "temperature_2m_min": row.get("temperature_2m_min"),
                "temperature_2m_max": row.get("temperature_2m_max"),
                "temperature_2m_mean": row.get("temperature_2m_mean"),
                "precipitation_sum": row.get("precipitation_sum"),
                "et0_fao_mm": row.get("et0_fao_mm"),
                "soil_temperature_0cm": row.get("soil_temperature_0cm"),
                "soil_temperature_6cm": row.get("soil_temperature_6cm"),
                "soil_temperature_18cm": row.get("soil_temperature_18cm"),
                "soil_temperature_54cm": row.get("soil_temperature_54cm"),
                "soil_moisture_0_1cm": row.get("soil_moisture_0_1cm"),
                "soil_moisture_1_3cm": row.get("soil_moisture_1_3cm"),
                "soil_moisture_3_9cm": row.get("soil_moisture_3_9cm"),
                "soil_moisture_9_27cm": row.get("soil_moisture_9_27cm"),
                "soil_moisture_27_81cm": row.get("soil_moisture_27_81cm"),
                "vapor_pressure_deficit": row.get("vapor_pressure_deficit"),
                "shortwave_radiation_sum": row.get("shortwave_radiation_sum"),
                "wind_speed_10m_max": row.get("wind_speed_10m_max"),
                "cloud_cover_mean": row.get("cloud_cover_mean"),
                "gdd_daily": row.get("gdd_daily"),
                "gdd_cumulative": row.get("gdd_cumulative"),
                "water_balance_30d_mm": row.get("water_balance_30d_mm"),
                "drought_index": row.get("drought_index"),
                "heat_stress_flag": row.get("heat_stress_flag"),
                "source": row.get("source") or "open-meteo",
                "model_used": row.get("model_used"),
            }
            if not params["field_id"] or not params["date"]:
                continue
            session.execute(text(UPSERT_WEATHER_SQL), params)
            n += 1
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return n


def apply_soil_payload(payload: dict[str, Any]) -> None:
    """Replace soil profile/layers/summary for field from inline payload."""
    field_id = payload.get("field_id")
    profile = payload.get("profile") or {}
    layers = payload.get("layers") or []
    summary = payload.get("summary") or {}
    if not field_id:
        raise ValueError("soil_profile payload missing field_id")

    session = SyncSession()
    try:
        # Delete old profiles (cascade layers) + summary for field
        old_ids = (
            session.execute(
                text(
                    "SELECT id FROM soil_profiles WHERE field_id = CAST(:fid AS uuid)"
                ),
                {"fid": field_id},
            )
            .scalars()
            .all()
        )
        for pid in old_ids:
            session.execute(
                text(
                    "DELETE FROM soil_field_summary WHERE profile_id = CAST(:pid AS uuid)"
                ),
                {"pid": str(pid)},
            )
            session.execute(
                text("DELETE FROM soil_profiles WHERE id = CAST(:pid AS uuid)"),
                {"pid": str(pid)},
            )
        session.execute(
            text("DELETE FROM soil_field_summary WHERE field_id = CAST(:fid AS uuid)"),
            {"fid": field_id},
        )

        profile_id = str(uuid.uuid4())
        fetched_at = profile.get("fetched_at") or datetime.utcnow().isoformat()
        session.execute(
            text(
                """
                INSERT INTO soil_profiles (
                  id, field_id, source, source_resolution_m,
                  fetched_at, metadata_json
                ) VALUES (
                  CAST(:id AS uuid), CAST(:field_id AS uuid),
                  :source, :resolution, CAST(:fetched_at AS timestamptz),
                  CAST(:metadata AS jsonb)
                )
                """
            ),
            {
                "id": profile_id,
                "field_id": field_id,
                "source": profile.get("source") or "soilgrids",
                "resolution": profile.get("source_resolution_m"),
                "fetched_at": fetched_at,
                "metadata": json.dumps(profile.get("metadata_json") or {}),
            },
        )
        for ld in layers:
            if not isinstance(ld, dict):
                continue
            session.execute(
                text(
                    """
                    INSERT INTO soil_layers (
                      id, profile_id, depth_top_cm, depth_bottom_cm,
                      sand_pct, silt_pct, clay_pct, ph, soc_g_kg, bd_kg_dm3,
                      cec_cmol_kg, nitrogen_g_kg, cfvo_pct,
                      fc_vol_pct, wp_vol_pct, awc_mm, ksat_cm_day, texture_class,
                      sand_q05, sand_q95, clay_q05, clay_q95,
                      ph_q05, ph_q95, soc_q05, soc_q95, ksat_q05, ksat_q95
                    ) VALUES (
                      CAST(:id AS uuid), CAST(:profile_id AS uuid),
                      :depth_top_cm, :depth_bottom_cm,
                      :sand_pct, :silt_pct, :clay_pct, :ph, :soc_g_kg, :bd_kg_dm3,
                      :cec_cmol_kg, :nitrogen_g_kg, :cfvo_pct,
                      :fc_vol_pct, :wp_vol_pct, :awc_mm, :ksat_cm_day, :texture_class,
                      :sand_q05, :sand_q95, :clay_q05, :clay_q95,
                      :ph_q05, :ph_q95, :soc_q05, :soc_q95, :ksat_q05, :ksat_q95
                    )
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "profile_id": profile_id,
                    "depth_top_cm": ld.get("depth_top_cm"),
                    "depth_bottom_cm": ld.get("depth_bottom_cm"),
                    "sand_pct": ld.get("sand_pct"),
                    "silt_pct": ld.get("silt_pct"),
                    "clay_pct": ld.get("clay_pct"),
                    "ph": ld.get("ph"),
                    "soc_g_kg": ld.get("soc_g_kg"),
                    "bd_kg_dm3": ld.get("bd_kg_dm3"),
                    "cec_cmol_kg": ld.get("cec_cmol_kg"),
                    "nitrogen_g_kg": ld.get("nitrogen_g_kg"),
                    "cfvo_pct": ld.get("cfvo_pct"),
                    "fc_vol_pct": ld.get("fc_vol_pct"),
                    "wp_vol_pct": ld.get("wp_vol_pct"),
                    "awc_mm": ld.get("awc_mm"),
                    "ksat_cm_day": ld.get("ksat_cm_day"),
                    "texture_class": ld.get("texture_class"),
                    "sand_q05": ld.get("sand_q05"),
                    "sand_q95": ld.get("sand_q95"),
                    "clay_q05": ld.get("clay_q05"),
                    "clay_q95": ld.get("clay_q95"),
                    "ph_q05": ld.get("ph_q05"),
                    "ph_q95": ld.get("ph_q95"),
                    "soc_q05": ld.get("soc_q05"),
                    "soc_q95": ld.get("soc_q95"),
                    "ksat_q05": ld.get("ksat_q05"),
                    "ksat_q95": ld.get("ksat_q95"),
                },
            )
        session.execute(
            text(
                """
                INSERT INTO soil_field_summary (
                  id, field_id, profile_id, dominant_texture, avg_ph,
                  total_soc_stock_t_ha, rootzone_awc_mm, drainage_class,
                  acidification_risk, compaction_risk, leaching_risk,
                  rooting_constraint, waterlogging_risk, topsoil_soc_stock_t_ha,
                  data_quality_score
                ) VALUES (
                  CAST(:id AS uuid), CAST(:field_id AS uuid), CAST(:profile_id AS uuid),
                  :dominant_texture, :avg_ph,
                  :total_soc_stock_t_ha, :rootzone_awc_mm, :drainage_class,
                  :acidification_risk, :compaction_risk, :leaching_risk,
                  :rooting_constraint, :waterlogging_risk, :topsoil_soc_stock_t_ha,
                  :data_quality_score
                )
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "field_id": field_id,
                "profile_id": profile_id,
                "dominant_texture": summary.get("dominant_texture"),
                "avg_ph": summary.get("avg_ph"),
                "total_soc_stock_t_ha": summary.get("total_soc_stock_t_ha"),
                "rootzone_awc_mm": summary.get("rootzone_awc_mm"),
                "drainage_class": summary.get("drainage_class"),
                "acidification_risk": summary.get("acidification_risk"),
                "compaction_risk": summary.get("compaction_risk"),
                "leaching_risk": summary.get("leaching_risk"),
                "rooting_constraint": summary.get("rooting_constraint"),
                "waterlogging_risk": summary.get("waterlogging_risk"),
                "topsoil_soc_stock_t_ha": summary.get("topsoil_soc_stock_t_ha"),
                "data_quality_score": payload.get("data_quality_score"),
            },
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _apply_assessment_job_progress(
    payload: dict[str, Any],
    *,
    status: str = "success",
) -> bool:
    """Update public.jobs from assessment_report ResultMessage when job_id present."""
    job_id = payload.get("job_id")
    if not job_id:
        return False
    ok = status == "success"
    if not ok:
        session = SyncSession()
        try:
            err = payload.get("error") or "assessment_report failed"
            row = session.execute(
                text(
                    """
                    UPDATE jobs
                    SET status = 'failed',
                        error = :error,
                        finished_at = COALESCE(finished_at, now()),
                        started_at = COALESCE(started_at, now())
                    WHERE id = CAST(:job_id AS uuid)
                    RETURNING id::text
                    """
                ),
                {"job_id": str(job_id), "error": str(err)[:2000]},
            ).first()
            session.commit()
            return bool(row)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    progress = {
        "stage": "done",
        "percent": 100,
        "object_key": payload.get("object_key"),
        "public_url": payload.get("public_url"),
        "filename": payload.get("filename"),
        "score": payload.get("score"),
        "grade": payload.get("grade"),
        "light": payload.get("light"),
        "one_liner": payload.get("one_liner"),
        "area_mu": payload.get("area_mu"),
        "content_type": payload.get("content_type") or "application/pdf",
    }
    for key in ("crop_type", "crop_name_zh"):
        if payload.get(key) is not None:
            progress[key] = payload[key]
    if isinstance(payload.get("scorecard"), dict):
        progress["scorecard"] = payload["scorecard"]
    session = SyncSession()
    try:
        row = session.execute(
            text(
                """
                UPDATE jobs
                SET status = 'succeeded',
                    progress_json = CAST(:progress AS jsonb),
                    error = NULL,
                    finished_at = COALESCE(finished_at, now()),
                    started_at = COALESCE(started_at, now())
                WHERE id = CAST(:job_id AS uuid)
                RETURNING id::text
                """
            ),
            {
                "job_id": str(job_id),
                "progress": json.dumps(progress, ensure_ascii=False),
            },
        ).first()
        session.commit()
        if row:
            logger.info(
                "assessment_job_updated job_id=%s object_key=%s",
                job_id,
                payload.get("object_key"),
            )
            return True
        logger.warning("assessment_job_missing_on_writer job_id=%s", job_id)
        return False
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _apply_domain_from_payload(
    payload: dict[str, Any] | None,
    *,
    status: str = "success",
) -> dict[str, Any]:
    stats: dict[str, Any] = {}
    if not payload or not isinstance(payload, dict):
        return stats
    kind = payload.get("kind")
    if kind == "weather_daily":
        if payload.get("oss_fallback") or payload.get("truncated"):
            stats["weather"] = "skipped_fallback_stub"
            return stats
        n = apply_weather_payload(payload)
        stats["weather_rows"] = n
    elif kind == "soil_profile":
        if payload.get("oss_fallback") or payload.get("truncated"):
            stats["soil"] = "skipped_fallback_stub"
            return stats
        apply_soil_payload(payload)
        stats["soil"] = "upserted"
    elif kind == "assessment_report":
        # PDF already on OSS. Cross-host: Job row lives on API/process DB —
        # update it here from ResultMessage payload (download host may lack Job).
        job_updated = _apply_assessment_job_progress(payload, status=status)
        stats["assessment_report"] = {
            "recorded": True,
            "job_id": payload.get("job_id"),
            "job_updated": job_updated,
            "object_key": payload.get("object_key"),
            "public_url": payload.get("public_url"),
            "score": payload.get("score"),
            "grade": payload.get("grade"),
            "filename": payload.get("filename"),
        }
    return stats


def handle_result_message(payload: dict[str, Any], meta: dict[str, Any]) -> None:
    try:
        msg = ResultMessage.model_validate(payload)
    except Exception as exc:
        logger.error("invalid_result_message err=%s", exc)
        return

    downloaded: dict[str, Any] = {}
    scene_upserts = 0
    for label, url in (msg.oss_urls or {}).items():
        if not url or not str(url).startswith("http"):
            downloaded[label] = {"skipped": True, "raw": url}
            continue
        url_l = str(url).lower().split("?", 1)[0]
        # Assessment PDFs (and other binaries) are link-only; do not GET as JSON.
        if label in ("assessment_pdf",) or url_l.endswith(".pdf"):
            downloaded[label] = {
                "link_only": True,
                "url": url,
                "content_type": "application/pdf",
            }
            continue
        data = _download_json(str(url))
        if data is None:
            downloaded[label] = {"download_failed": True, "url": url}
            continue
        downloaded[label] = data

        # Oversized weather/soil fallback body
        if isinstance(data, dict) and data.get("kind") in (
            "weather_daily",
            "soil_profile",
        ):
            try:
                stats = _apply_domain_from_payload(data, status=msg.status)
                downloaded[label] = {"applied": True, **stats}
            except Exception as exc:
                logger.exception(
                    "domain_apply_from_oss_failed label=%s err=%s", label, exc
                )
                downloaded[label] = {"apply_failed": True, "error": str(exc)[:300]}
            continue

        if _is_lonlat_scene_product(data):
            try:
                key = None
                if isinstance(data, dict):
                    key = data.get("json_oss_key")
                apply_parcel_scene_product(data, json_oss_key=key)
                scene_upserts += 1
            except Exception as exc:
                logger.exception(
                    "parcel_scene_upsert_failed label=%s err=%s", label, exc
                )
                downloaded[label] = {
                    "download_ok": True,
                    "upsert_failed": True,
                    "error": str(exc)[:300],
                }

    domain_stats: dict[str, Any] = {}
    try:
        inline_payload = msg.payload
        if (
            isinstance(inline_payload, dict)
            and inline_payload.get("kind") == "assessment_report"
            and msg.status != "success"
            and msg.error
            and not inline_payload.get("error")
        ):
            inline_payload = {**inline_payload, "error": msg.error}
        # Prefer extras.job_id when payload omitted it (cross-host failure path).
        if (
            isinstance(inline_payload, dict)
            and inline_payload.get("kind") == "assessment_report"
            and not inline_payload.get("job_id")
            and isinstance(msg.extras, dict)
            and msg.extras.get("job_id")
        ):
            inline_payload = {**inline_payload, "job_id": msg.extras.get("job_id")}
        domain_stats = _apply_domain_from_payload(inline_payload, status=msg.status)
    except Exception as exc:
        logger.exception(
            "domain_apply_from_inline_failed task_id=%s err=%s", msg.task_id, exc
        )
        domain_stats = {"inline_apply_error": str(exc)[:300]}

    upsert_mq_task_result(msg, {**downloaded, "domain": domain_stats})
    logger.info(
        "mq_result_written task_id=%s status=%s urls=%s scenes=%s domain=%s retry=%s",
        msg.task_id,
        msg.status,
        list(msg.oss_urls.keys()),
        scene_upserts,
        domain_stats,
        meta.get("retry_count"),
    )
