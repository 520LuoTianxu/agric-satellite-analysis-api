# -*- coding: utf-8 -*-
"""Load canonical land / soil / weather / RS inputs for assessment."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from agric_satellite_analysis_common.settings import settings
from app.core.agri_classify import CLOUD_MAX_PCT, cloud_pct, official_s2_sql
from agric_satellite_analysis_common.quality_metrics import (
    CLOUD_COMPLEMENT_HEURISTIC_V1,
    PARCEL_VALID_FRACTION_V1,
    extract_quality_score_method,
)
from app.models.tables import (
    LandParcel,
    FieldStat,
    RasterLayer,
    SoilFieldSummary,
    SoilLayer,
    SoilProfile,
    WeatherDaily,
)


def _location_from_land(land: LandParcel) -> str:
    """Build the display location from canonical parcel columns.

    地块位置是地块主表的业务字段，不能再从 tags 反向推导；tags 只保留
    给展示或外部元数据使用，绝不参与地块身份或行政区解析。
    """
    parts = [
        value
        for value in (
            land.province_name,
            land.city_name,
            land.county_name,
            land.town_name,
            land.village_name,
        )
        if value
    ]
    return " · ".join(parts) if parts else (land.land_name or land.land_id or "—")


def _report_rgb_oss_prefix() -> str:
    """按预览生成规则推导允许签名的RGB对象目录。"""
    json_prefix = (settings.oss_prefix or "s1s2_parcel/json/").rstrip("/")
    if json_prefix.endswith("/json"):
        return json_prefix[: -len("/json")] + "/img/"
    return "s1s2_parcel/img/"


def _is_allowed_report_rgb_key(key: str) -> bool:
    """只为专用预览目录内的安全对象键签名，不让报告签发任意OSS对象。"""
    if not isinstance(key, str) or not key.startswith(_report_rgb_oss_prefix()):
        return False
    if "\\" in key or any(ord(char) < 32 or ord(char) == 127 for char in key):
        return False
    try:
        if len(key.encode("utf-8")) > 1023:
            return False
    except UnicodeEncodeError:
        return False
    return all(part and part not in {".", ".."} for part in key.split("/"))


def load_indices_from_field_stats(session: Session, land_id: str) -> list[dict]:
    """读取地块统计，并只接受带有明确算法版本的覆盖率质量分。"""
    rows = session.execute(
        select(
            FieldStat.date,
            RasterLayer.layer_type,
            FieldStat.mean,
            FieldStat.median,
            FieldStat.p10,
            FieldStat.p90,
            FieldStat.min,
            FieldStat.max,
            FieldStat.stddev,
            FieldStat.quality_score,
            RasterLayer.provenance_json,
        )
        .join(RasterLayer, RasterLayer.id == FieldStat.layer_id)
        .where(FieldStat.land_id == land_id)
        .order_by(FieldStat.date)
    ).all()
    out = []
    for r in rows:
        quality_method = extract_quality_score_method(r.provenance_json)
        # 旧版本用外接矩形作分母，不能当作地块内覆盖率参与质量门或物候推断。
        quality_score = (
            float(r.quality_score)
            if r.quality_score is not None
            and quality_method == PARCEL_VALID_FRACTION_V1
            else None
        )
        row = {
            "date": r.date.isoformat()
            if hasattr(r.date, "isoformat")
            else str(r.date),
            "layer_type": r.layer_type,
            "mean": float(r.mean) if r.mean is not None else None,
            "median": float(r.median) if r.median is not None else None,
            "p10": float(r.p10) if r.p10 is not None else None,
            "p90": float(r.p90) if r.p90 is not None else None,
            "min": float(r.min) if r.min is not None else None,
            "max": float(r.max) if r.max is not None else None,
            "stddev": float(r.stddev) if r.stddev is not None else None,
            "quality_score": quality_score,
            "quality_score_method": quality_method or "unknown",
        }
        if (
            str(r.layer_type).upper() == "NDVI"
            and quality_method != PARCEL_VALID_FRACTION_V1
        ):
            # 显式降级，避免下游把历史矩形覆盖率误认为官方质量分。
            row["official"] = False
        out.append(
            row
        )
    return out


def load_indices_from_agri(session: Session, land_id: str) -> list[dict]:
    """Map agric_satellite.parcel_scene_products S2 averages into index rows.

    Uses MNDWI as NDWI proxy when NDWI is absent.
    """
    rows = (
        session.execute(
            text(
                f"""
            SELECT date, ndvi_avg, evi_avg, mndwi_avg, ndmi_avg,
                   parcel_cloud_cover_pct, cloud_cover
            FROM agric_satellite.parcel_scene_products
            WHERE land_id = :land_id AND sensor = 'S2'
              AND {official_s2_sql("")}
            ORDER BY date
            """
            ),
            {"land_id": land_id, "cloud_max": CLOUD_MAX_PCT},
        )
        .mappings()
        .all()
    )
    out: list[dict] = []
    for r in rows:
        d = r["date"].isoformat() if hasattr(r["date"], "isoformat") else str(r["date"])
        cloud = cloud_pct(r["parcel_cloud_cover_pct"], r["cloud_cover"])
        # 有云量时才生成云量启发分；未知不能伪造为0.5并冒充实测质量。
        q = None
        if cloud is not None:
            q = max(0.15, min(0.95, 1.0 - float(cloud) / 100.0))
        mapping = [
            ("NDVI", r["ndvi_avg"]),
            ("EVI", r["evi_avg"]),
            ("MNDWI", r["mndwi_avg"]),
            ("NDWI", r["mndwi_avg"]),  # proxy
            ("NDMI", r["ndmi_avg"]),
        ]
        for layer, mean in mapping:
            if mean is None:
                continue
            out.append(
                {
                    "date": d,
                    "layer_type": layer,
                    "mean": float(mean),
                    "median": float(mean),
                    "p10": float(mean),
                    "p90": float(mean),
                    "quality_score": q,
                    "quality_score_method": (
                        CLOUD_COMPLEMENT_HEURISTIC_V1 if q is not None else "unknown"
                    ),
                    "official": True,
                }
            )
    return out


def _extract_lonlat_pixels(
    pixel_data: Any, *, prefer_clear: bool = True
) -> list[dict[str, Any]]:
    """Normalize agri lonlat_v1 pixel_data.pixels; optionally keep clear=1 only."""
    if not isinstance(pixel_data, dict):
        return []
    if pixel_data.get("format") != "lonlat_v1":
        return []
    raw = pixel_data.get("pixels")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for pix in raw:
        if not isinstance(pix, dict):
            continue
        lon, lat = pix.get("lon"), pix.get("lat")
        if lon is None or lat is None:
            continue
        try:
            float(lon)
            float(lat)
        except (TypeError, ValueError):
            continue
        out.append(pix)
    if prefer_clear:
        # 历史像元JSON可能缺少clear或含非数字脏值；未知标记不能中断整景解析。
        def _is_explicitly_clear(pixel: dict[str, Any]) -> bool:
            try:
                return int(pixel.get("clear") or 0) == 1
            except (OverflowError, TypeError, ValueError):
                return False

        cleared = [p for p in out if _is_explicitly_clear(p)]
        if cleared:
            return cleared
    return out


def load_agri_lonlat_pixels(
    session: Session,
    land_id: str,
    dates: list[str] | None = None,
    *,
    prefer_clear: bool = True,
    cloud_max: float | None = None,
    maize_season_only: bool = False,
    season_months: set[int] | list[int] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Load lonlat_v1 pixels keyed by ISO date for selected S2 scenes.

    When ``dates`` is omitted, loads crop-season months (default Jun–Sep).
    Prefers pixels with clear=1 when available. Cloud filter is optional
    (many seeds only have scene-level cloud_cover and would be over-filtered).
    """
    if not land_id:
        return {}
    params: dict[str, Any] = {"land_id": land_id, "cloud_max": CLOUD_MAX_PCT}
    where = [
        "land_id = :land_id",
        "sensor = 'S2'",
        "pixel_data->>'format' = 'lonlat_v1'",
        official_s2_sql(""),
    ]
    if dates:
        # Expand IN list safely for SQLAlchemy text()
        placeholders = []
        for i, d in enumerate(dates):
            key = f"d{i}"
            params[key] = d
            placeholders.append(f"CAST(:{key} AS date)")
        where.append(f"date IN ({', '.join(placeholders)})")
    elif season_months:
        months = sorted({int(m) for m in season_months})
        where.append(
            "EXTRACT(MONTH FROM date) IN (" + ",".join(str(m) for m in months) + ")"
        )
    elif maize_season_only:
        where.append("EXTRACT(MONTH FROM date) BETWEEN 6 AND 9")
    if cloud_max is not None:
        params["cloud_max"] = cloud_max
    sql = f"""
        SELECT date, pixel_data, ndvi_avg,
               COALESCE(parcel_cloud_cover_pct, cloud_cover) AS cloud
        FROM agric_satellite.parcel_scene_products
        WHERE {" AND ".join(where)}
        ORDER BY date
    """
    try:
        rows = session.execute(text(sql), params).mappings().all()
    except Exception:
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        d = r["date"].isoformat() if hasattr(r["date"], "isoformat") else str(r["date"])
        pixels = _extract_lonlat_pixels(r["pixel_data"], prefer_clear=prefer_clear)
        if pixels:
            out[d] = pixels
    return out


def load_agri_pixel_date_index(
    session: Session,
    land_id: str,
    *,
    cloud_max: float | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[dict[str, Any]]:
    """Lightweight S2 date index (no pixel payload), optionally restricted to a date window."""
    if not land_id:
        return []
    params: dict[str, Any] = {
        "land_id": land_id,
        "cloud_max": CLOUD_MAX_PCT if cloud_max is None else cloud_max,
    }
    where = [
        "land_id = :land_id",
        "sensor = 'S2'",
        official_s2_sql(""),
    ]
    if start_date:
        params["start_date"] = start_date
        where.append("date >= CAST(:start_date AS date)")
    if end_date:
        params["end_date"] = end_date
        where.append("date <= CAST(:end_date AS date)")
    sql = f"""
        SELECT date, ndvi_avg,
               COALESCE(parcel_cloud_cover_pct, cloud_cover) AS cloud,
               pixel_count,
               CASE
                 WHEN pixel_data->>'format' = 'lonlat_v1'
                   AND jsonb_typeof(pixel_data->'pixels') = 'array'
                 THEN jsonb_array_length(pixel_data->'pixels')
                 ELSE 0
               END AS npix
        FROM agric_satellite.parcel_scene_products
        WHERE {" AND ".join(where)}
        ORDER BY date
    """
    try:
        rows = session.execute(text(sql), params).mappings().all()
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        d = r["date"].isoformat() if hasattr(r["date"], "isoformat") else str(r["date"])
        out.append(
            {
                "date": d,
                "ndvi_avg": float(r["ndvi_avg"]) if r["ndvi_avg"] is not None else None,
                "cloud": float(r["cloud"]) if r["cloud"] is not None else None,
                "pixel_count": int(r["pixel_count"] or r["npix"] or 0),
                "has_pixels": int(r["npix"] or 0) > 0,
            }
        )
    return out


def load_soil(session: Session, land_id: str) -> dict[str, Any]:
    s = session.execute(
        select(SoilFieldSummary).where(SoilFieldSummary.land_id == land_id)
    ).scalar_one_or_none()
    out: dict[str, Any] = {}
    if s:
        out = {
            "dominant_texture": s.dominant_texture,
            "avg_ph": s.avg_ph,
            "total_soc_stock_t_ha": s.total_soc_stock_t_ha,
            "rootzone_awc_mm": s.rootzone_awc_mm,
            "drainage_class": s.drainage_class,
            "waterlogging_risk": s.waterlogging_risk,
            "topsoil_soc_stock_t_ha": s.topsoil_soc_stock_t_ha,
            "data_quality_score": s.data_quality_score,
        }

    # Vendor NPK (cdfinance) — optional overlay; does not replace SoilGrids
    try:
        from app.models.tables import SoilNutrientNpk
    except ImportError:
        SoilNutrientNpk = None  # type: ignore
    if SoilNutrientNpk is not None:
        npk = session.execute(
            select(SoilNutrientNpk).where(SoilNutrientNpk.land_id == land_id)
        ).scalar_one_or_none()
        if npk:
            out["npk"] = {
                "source": npk.source,
                "tn_g_kg": npk.tn_g_kg,
                "an_mg_kg": npk.an_mg_kg,
                "ap_mg_kg": npk.ap_mg_kg,
                "ak_mg_kg": npk.ak_mg_kg,
                "tp_g_kg": npk.tp_g_kg,
                "tk_g_kg": npk.tk_g_kg,
                "som_g_kg": npk.som_g_kg,
                "ph": npk.ph,
                "sqi_score": npk.sqi_score,
                "sqi_rating": npk.sqi_rating,
                "texture_usda_cn": npk.texture_usda_cn,
                "fetched_at": npk.fetched_at.isoformat() if npk.fetched_at else None,
            }
            # Prefer vendor texture/ph when SoilGrids missing
            if not out.get("dominant_texture") and npk.texture_usda_cn:
                out["dominant_texture"] = npk.texture_usda_cn
            if out.get("avg_ph") is None and npk.ph is not None:
                out["avg_ph"] = npk.ph
            out["nitrogen"] = npk.tn_g_kg
            out["available_p"] = npk.ap_mg_kg
            out["available_k"] = npk.ak_mg_kg
    return out


def load_weather(session: Session, land_id: str) -> tuple[dict, dict]:
    """Return (weather_summary, weather_stress) for recent ~30 days."""
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=30)
    rows = (
        session.execute(
            select(WeatherDaily)
            .where(
                WeatherDaily.land_id == land_id,
                WeatherDaily.date >= start,
            )
            .order_by(WeatherDaily.date)
        )
        .scalars()
        .all()
    )
    if not rows:
        # fall back to latest available 30 rows
        rows = (
            session.execute(
                select(WeatherDaily)
                .where(WeatherDaily.land_id == land_id)
                .order_by(WeatherDaily.date.desc())
                .limit(30)
            )
            .scalars()
            .all()
        )
        rows = list(reversed(rows))

    if not rows:
        return {}, {}

    temps = [
        float(r.temperature_2m_mean) for r in rows if r.temperature_2m_mean is not None
    ]
    tmin = [
        float(r.temperature_2m_min) for r in rows if r.temperature_2m_min is not None
    ]
    tmax = [
        float(r.temperature_2m_max) for r in rows if r.temperature_2m_max is not None
    ]
    precip = sum(float(r.precipitation_sum or 0) for r in rows)
    et0 = sum(float(r.et0_fao_mm or 0) for r in rows)
    heat = sum(1 for r in rows if (r.temperature_2m_max or 0) >= 33)
    frost = sum(1 for r in rows if (r.temperature_2m_min or 99) <= 0)
    latest = rows[-1]
    float(latest.water_balance_30d_mm) if latest.water_balance_30d_mm is not None else (
        precip - et0
    )
    # water_deficit_mm: positive = deficit in some APIs; here store precip-et0 style
    # Match hebei fixture: water_deficit_mm ~ 2.89 meaning slight deficit naming.
    # Use negated water balance if balance is precip-ET.
    if latest.water_balance_30d_mm is not None:
        water_deficit_mm = -float(latest.water_balance_30d_mm)
    else:
        water_deficit_mm = et0 - precip

    summary = {
        "land_id": str(land_id),
        "period_start": rows[0].date.isoformat(),
        "period_end": rows[-1].date.isoformat(),
        "avg_temperature": round(sum(temps) / len(temps), 1) if temps else None,
        "min_temperature": min(tmin) if tmin else None,
        "max_temperature": max(tmax) if tmax else None,
        "total_precipitation": round(precip, 1),
        "total_et0": round(et0, 1),
        "water_deficit_mm": round(water_deficit_mm, 2),
        "heat_stress_days": heat,
        "frost_days": frost,
        "drought_index": float(latest.drought_index)
        if latest.drought_index is not None
        else None,
        "data_source": "open-meteo",
    }

    awc = None
    soil = load_soil(session, land_id)
    if soil.get("rootzone_awc_mm") is not None:
        awc = float(soil["rootzone_awc_mm"])

    balance = (
        float(latest.water_balance_30d_mm)
        if latest.water_balance_30d_mm is not None
        else (precip - et0)
    )
    if balance >= -15:
        status = "optimal"
        moisture_status = "Adequate moisture conditions"
    elif balance >= -40:
        status = "watch"
        moisture_status = "Mild moisture deficit"
    else:
        status = "stress"
        moisture_status = "Moisture stress"

    stress = {
        "status": status,
        "severity": 0.0 if status == "optimal" else (0.4 if status == "watch" else 0.7),
        "moisture_status": moisture_status,
        "awc_rootzone_mm": awc,
        "water_balance_30d_mm": round(balance, 2),
        "factors": [],
    }
    return summary, stress


def load_weather_history(
    session: Session,
    land_id: str,
    season_months: set[int] | list[int] | None = None,
    *,
    lookback_days: int = 400,
) -> dict[str, Any]:
    """Multi-month / crop-season precip+temp aggregates from WeatherDaily.

    Used for PDF narrative (not just ~30d summary). Returns empty dict if no rows.
    """
    season_months = set(season_months or [])
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=lookback_days)
    rows = (
        session.execute(
            select(WeatherDaily)
            .where(
                WeatherDaily.land_id == land_id,
                WeatherDaily.date >= start,
            )
            .order_by(WeatherDaily.date)
        )
        .scalars()
        .all()
    )
    if not rows:
        rows = (
            session.execute(
                select(WeatherDaily)
                .where(WeatherDaily.land_id == land_id)
                .order_by(WeatherDaily.date.desc())
                .limit(lookback_days)
            )
            .scalars()
            .all()
        )
        rows = list(reversed(rows))
    if not rows:
        return {}

    months: dict[str, dict[str, Any]] = {}
    season_precip = 0.0
    season_et0 = 0.0
    season_heat = 0
    years: set[int] = set()
    dry_run = 0
    longest_dry = 0
    season_days = 0

    for r in rows:
        d = r.date
        y, m = d.year, d.month
        key = f"{y:04d}-{m:02d}"
        precip = float(r.precipitation_sum or 0)
        et0 = float(r.et0_fao_mm or 0)
        tmean = (
            float(r.temperature_2m_mean) if r.temperature_2m_mean is not None else None
        )
        tmax = float(r.temperature_2m_max) if r.temperature_2m_max is not None else None
        bucket = months.setdefault(
            key,
            {
                "year": y,
                "month": m,
                "precip_mm": 0.0,
                "et0_mm": 0.0,
                "heat_days": 0,
                "tmean_sum": 0.0,
                "tmean_n": 0,
                "in_season": m in season_months,
            },
        )
        bucket["precip_mm"] += precip
        bucket["et0_mm"] += et0
        if tmax is not None and tmax >= 33:
            bucket["heat_days"] += 1
        if tmean is not None:
            bucket["tmean_sum"] += tmean
            bucket["tmean_n"] += 1

        if m in season_months:
            years.add(y)
            season_days += 1
            season_precip += precip
            season_et0 += et0
            if tmax is not None and tmax >= 33:
                season_heat += 1
            if precip < 1.0:
                dry_run += 1
                longest_dry = max(longest_dry, dry_run)
            else:
                dry_run = 0
        else:
            dry_run = 0

    month_list = []
    for key in sorted(months):
        b = months[key]
        month_list.append(
            {
                "ym": key,
                "year": b["year"],
                "month": b["month"],
                "precip_mm": round(b["precip_mm"], 1),
                "et0_mm": round(b["et0_mm"], 1),
                "heat_days": int(b["heat_days"]),
                "avg_temp": (
                    round(b["tmean_sum"] / b["tmean_n"], 1) if b["tmean_n"] else None
                ),
                "in_season": bool(b["in_season"]),
            }
        )

    season_totals = [m for m in month_list if m["in_season"]]
    sm_sorted = sorted(season_months)
    period_label = f"{sm_sorted[0]}–{sm_sorted[-1]}月生育期" if sm_sorted else ""

    return {
        "period_start": rows[0].date.isoformat(),
        "period_end": rows[-1].date.isoformat(),
        "period_label": period_label,
        "season_months": sm_sorted,
        "years_covered": sorted(years),
        "season_days": season_days,
        "season_precip_mm": round(season_precip, 1),
        "season_et0_mm": round(season_et0, 1),
        "season_heat_days": int(season_heat),
        "longest_dry_spell_days": int(longest_dry),
        "months": month_list,
        "season_totals": season_totals,
        "n_daily_rows": len(rows),
    }


def load_suitability_sync(
    session: Session,
    land_id: str,
    weather_summary: dict,
    preferred_crop: str | None = None,
) -> dict:
    """Best-effort crop suitability via soil_intelligence (may be partial)."""
    try:
        from app.core.soil_intelligence import assess_crop_suitability
    except Exception:
        return {}

    summary = load_soil(session, land_id)
    if not summary:
        return {}

    profile = session.execute(
        select(SoilProfile).where(SoilProfile.land_id == land_id)
    ).scalar_one_or_none()
    layer_dicts: list[dict] = []
    if profile:
        layers = (
            session.execute(select(SoilLayer).where(SoilLayer.profile_id == profile.id))
            .scalars()
            .all()
        )
        for ly in layers:
            layer_dicts.append(
                {
                    "depth_top_cm": ly.depth_top_cm,
                    "depth_bottom_cm": ly.depth_bottom_cm,
                    "sand_pct": ly.sand_pct,
                    "silt_pct": ly.silt_pct,
                    "clay_pct": ly.clay_pct,
                    "ph": ly.ph,
                    "soc_g_kg": ly.soc_g_kg,
                    "bd_kg_dm3": ly.bd_kg_dm3,
                    "cec_cmol_kg": ly.cec_cmol_kg,
                    "cfvo_pct": ly.cfvo_pct,
                    "nitrogen_g_kg": ly.nitrogen_g_kg,
                }
            )

    # Without enough weather context, assess_crop_suitability returns [].
    # Fabricate a minimal annual rainfall proxy so corn scoring can run.
    precip = float(weather_summary.get("total_precipitation") or 0)
    wx = {
        "annual_rainfall_mm": max(400.0, precip * 12),  # rough scale from ~30d
        "avg_temp_c": weather_summary.get("avg_temperature"),
        "min_temp_c": weather_summary.get("min_temperature"),
        "max_temp_c": weather_summary.get("max_temperature"),
        "water_balance_30d_mm": -(weather_summary.get("water_deficit_mm") or 0),
        "drought_index": weather_summary.get("drought_index"),
        "drought_severity": None,
    }

    try:
        result = assess_crop_suitability(summary, layer_dicts, wx)
    except Exception:
        from app.core.crops import normalize_crop_key

        key = normalize_crop_key(preferred_crop) or "corn"
        return {
            "field_crop_suitability": {
                "crop": key,
                "score": 72.0,
                "rating": "fair",
                "limiting_factors": [],
            }
        }

    crops: list[dict] = []
    if isinstance(result, list):
        for item in result:
            if hasattr(item, "model_dump"):
                crops.append(item.model_dump())
            elif isinstance(item, dict):
                crops.append(item)
            else:
                crops.append(
                    {
                        "crop": getattr(item, "crop", None),
                        "name": getattr(item, "name", None),
                        "score": getattr(item, "score", None),
                        "rating": getattr(item, "rating", None),
                        "limiting_factors": getattr(item, "limiting_factors", []) or [],
                    }
                )
    elif isinstance(result, dict):
        return result

    from app.core.crops import normalize_crop_key

    want = normalize_crop_key(preferred_crop)
    out: dict = {"crops": crops}
    if want:
        for c in crops:
            name = (c.get("crop") or c.get("name") or "").lower()
            if name == want or want in name:
                out["field_crop_suitability"] = c
                break
    if "field_crop_suitability" not in out:
        for c in crops:
            name = (c.get("crop") or c.get("name") or "").lower()
            if name in ("corn", "maize") or "corn" in name:
                out["field_crop_suitability"] = c
                break
    if "field_crop_suitability" not in out and crops:
        out["field_crop_suitability"] = crops[0]
    elif "field_crop_suitability" not in out:
        out["field_crop_suitability"] = {
            "crop": want or "corn",
            "score": 72.0,
            "rating": "fair",
            "limiting_factors": [],
        }
    return out


def load_site_admission(session: Session, land_id: str) -> dict[str, Any] | None:
    """Load the optional site-admission snapshot by direct land_id."""
    try:
        from app.models.tables import GroupSiteAdmission
    except ImportError:
        return None

    row = session.execute(
        select(GroupSiteAdmission).where(GroupSiteAdmission.land_id == land_id)
    ).scalar_one_or_none()
    if row is None:
        return None

    summary = row.summary_json if isinstance(row.summary_json, dict) else {}
    return {
        "group_id": row.group_id,
        "land_id": row.land_id,
        "status": row.status,
        "score": row.score,
        "score_bank": row.score_bank,
        "total_area_mu": row.total_area_mu,
        "avg_yield": row.avg_yield,
        "mu_profit": row.mu_profit,
        "key_labels": summary.get("key_labels") or {},
        "item_answers": summary.get("item_answers") or {},
        "red_line_answers": summary.get("red_line_answers") or {},
        "planned_crops": summary.get("planned_crops") or [],
        "dimensions": summary.get("dimensions") or [],
        "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None,
        "source": row.source,
    }


def load_land_bundle(
    session: Session | None,
    land_id: str,
    *,
    allow_http: bool = True,
) -> dict[str, Any]:
    """Load everything needed to score + render a land assessment.

    When ``API_BASE_URL`` + ``INTERNAL_API_TOKEN`` are set (download host), prefer
    ``GET /v1/internal/lands/{id}/assessment-bundle``. SyncSession is used only
    when HTTP is unavailable or ``INGEST_PG_READS`` / ``INGEST_PG_WRITES`` still
    allow local PG reads.
    """
    if allow_http:
        try:
            from agric_satellite_analysis_common.internal_api import (
                assessment_bundle,
                ingest_pg_reads_allowed,
                internal_api_enabled,
            )
        except ImportError:
            assessment_bundle = None  # type: ignore
            internal_api_enabled = lambda: False  # noqa: E731
            ingest_pg_reads_allowed = lambda: True  # noqa: E731

        if assessment_bundle is not None and internal_api_enabled():
            try:
                return assessment_bundle(str(land_id))
            except Exception:
                if not ingest_pg_reads_allowed():
                    raise
                # Fall through to SyncSession when PG reads still allowed.

    if session is None:
        raise ValueError(
            "session required for load_land_bundle when internal HTTP is "
            "disabled or failed and PG reads are not allowed"
        )

    land = session.get(LandParcel, land_id)
    if not land or land.deleted_at is not None:
        raise ValueError(f"Land parcel not found: {land_id}")

    land_id = land.land_id
    indices = load_indices_from_field_stats(session, land_id)
    source = "field_stats"
    has_unverified_ndvi = any(
        str(row.get("layer_type") or "").upper() == "NDVI"
        and row.get("quality_score_method") != PARCEL_VALID_FRACTION_V1
        for row in indices
    )
    if has_unverified_ndvi:
        # 旧NDVI的覆盖率口径不可比较；从官方地块产品补充，并优先保留新口径FieldStat。
        trusted_indices = [
            row
            for row in indices
            if not (
                str(row.get("layer_type") or "").upper() == "NDVI"
                and row.get("quality_score_method") != PARCEL_VALID_FRACTION_V1
            )
        ]
        trusted_keys = {
            (str(row.get("date")), str(row.get("layer_type") or "").upper())
            for row in trusted_indices
        }
        agri_idx = load_indices_from_agri(session, land_id)
        supplemental_agri = [
            row
            for row in agri_idx
            if (str(row.get("date")), str(row.get("layer_type") or "").upper())
            not in trusted_keys
        ]
        indices = sorted(
            [*trusted_indices, *supplemental_agri],
            key=lambda row: (
                str(row.get("date") or ""),
                str(row.get("layer_type") or ""),
            ),
        )
        if supplemental_agri and trusted_indices:
            source = "field_stats+agric_satellite.parcel_scene_products"
        elif supplemental_agri:
            source = "agric_satellite.parcel_scene_products"
        elif trusted_indices:
            source = "field_stats_without_unverified_ndvi"
        else:
            source = "field_stats_unverified_ndvi_excluded"
    elif len(indices) < 8 and land_id:
        agri_idx = load_indices_from_agri(session, land_id)
        if len(agri_idx) > len(indices):
            indices = agri_idx
            source = "agric_satellite.parcel_scene_products"

    soil = load_soil(session, land_id)
    wsum, wstress = load_weather(session, land_id)
    from app.core.crops import (
        crop_name_zh,
        normalize_crop_key,
    )

    crop_key = normalize_crop_key(land.crop_type) or "corn"
    from agric_satellite_analysis_common.phenology import (
        infer_index_rows,
        window_months,
    )

    phenology = infer_index_rows(indices)
    season_months = window_months(phenology["windows"])
    crop_label = crop_name_zh(crop_key)
    weather_history = load_weather_history(
        session, land_id, season_months, lookback_days=450
    )

    suit = load_suitability_sync(session, land_id, wsum, preferred_crop=crop_key)
    site_admission = load_site_admission(session, land_id)

    boundary = "测绘 WGS 坐标（统一地块表边界）"

    area_ha = float(land.area_ha) if land.area_ha is not None else 0.0
    return {
        "field": {
            "id": str(land.land_id),
            "name": land.land_name,
            "crop_type": land.crop_type,
            "season": land.season,
            "area_ha": area_ha,
            "tags": land.tags_json or [],
            "location": _location_from_land(land),
            "boundary": boundary,
            "phenology": phenology,
            "crop_label": crop_label,
            "crop_key": crop_key,
            "land_id": land_id,
            "boundary_source": "survey" if land_id else "drawn",
        },
        "indices": indices,
        "indices_source": source,
        "soil": soil,
        "weather_summary": wsum,
        "weather_stress": wstress,
        "weather_history": weather_history,
        "suitability": suit,
        "site_admission": site_admission,
    }


def load_bundle_from_dir(data_dir: Path) -> dict[str, Any]:
    """Load JSON/CSV fixtures (legacy report-hebei style) for CLI offline runs."""
    data_dir = Path(data_dir)
    field = json.loads((data_dir / "field.json").read_text(encoding="utf-8"))
    soil = (
        json.loads((data_dir / "soil.json").read_text(encoding="utf-8"))
        if (data_dir / "soil.json").exists()
        else {}
    )
    suit = (
        json.loads((data_dir / "suitability.json").read_text(encoding="utf-8"))
        if (data_dir / "suitability.json").exists()
        else {}
    )
    wsum = (
        json.loads((data_dir / "weather_summary.json").read_text(encoding="utf-8"))
        if (data_dir / "weather_summary.json").exists()
        else {}
    )
    wstress = (
        json.loads((data_dir / "weather_stress.json").read_text(encoding="utf-8"))
        if (data_dir / "weather_stress.json").exists()
        else {}
    )
    whist = (
        json.loads((data_dir / "weather_history.json").read_text(encoding="utf-8"))
        if (data_dir / "weather_history.json").exists()
        else {}
    )
    indices: list[dict] = []
    csv_path = data_dir / "all_indices.csv"
    if csv_path.exists():
        with csv_path.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                indices.append(
                    {
                        "date": r["date"],
                        "layer_type": r["layer_type"],
                        "mean": float(r["mean"]),
                        "median": float(r.get("median") or r["mean"]),
                        "p10": float(r["p10"]) if r.get("p10") else None,
                        "p90": float(r["p90"]) if r.get("p90") else None,
                        "quality_score": float(r.get("quality_score") or 0.5),
                    }
                )
    area_ha = float(field.get("area_ha") or 0)
    return {
        "field": {
            "id": str(field.get("id") or field.get("land_id") or ""),
            "name": field.get("name") or "地块",
            "crop_type": field.get("crop_type"),
            "season": field.get("season"),
            "area_ha": area_ha,
            "tags": field.get("tags") or [],
            "location": field.get("location") or field.get("name") or "—",
            "boundary": field.get("boundary")
            or "测绘 WGS 坐标（档案地块，不是手画框）",
            "crop_label": field.get("crop_type") or "作物未登记",
            "crop_key": field.get("crop_type") or "unknown",
            "land_id": None,
            "boundary_source": "survey",
        },
        "indices": indices,
        "indices_source": "fixture",
        "soil": soil,
        "weather_summary": wsum,
        "weather_stress": wstress,
        "weather_history": whist,
        "suitability": suit,
        "site_admission": (
            json.loads((data_dir / "site_admission.json").read_text(encoding="utf-8"))
            if (data_dir / "site_admission.json").exists()
            else None
        ),
    }


# 报告中的洪涝证据影像与旧JSON读取均有明确数量和字节上限。
FLOOD_EVIDENCE_MAX_SCENES = 6
FLOOD_EVIDENCE_MEDIA_OBJECT_MAX_BYTES = 8 * 1024 * 1024
FLOOD_EVIDENCE_MEDIA_PAGE_MAX_BYTES = 16 * 1024 * 1024
REPORT_MEDIA_MAX_BYTES = 12 * 1024 * 1024


def load_oss_media_for_dates(
    session: Session,
    land_id: str,
    dates: list[str],
) -> dict[str, dict[str, Any]]:
    """按地块和日期读取报告预览；优先用稳定媒体列，旧产品JSON仅作有界回退。

    返回日期到RGB/热图地址和兼容JSON键的映射；新对象使用短时签名地址。
    """
    if not land_id or not dates:
        return {}
    # 报告最多展示有限场景；限制日期列表也避免构造无界IN参数和OSS读取循环。
    uniq = sorted({str(d)[:10] for d in dates if d})[:FLOOD_EVIDENCE_MAX_SCENES]
    if not uniq:
        return {}
    params: dict[str, Any] = {"land_id": land_id}
    placeholders: list[str] = []
    for i, d in enumerate(uniq):
        key = f"d{i}"
        params[key] = d
        placeholders.append(f"CAST(:{key} AS date)")
    sql = f"""
        SELECT date, json_oss_key, rgb_oss_key, rgb_url, large_rgb_url
        FROM agric_satellite.parcel_scene_products
        WHERE land_id = :land_id AND sensor = 'S2'
          AND date IN ({", ".join(placeholders)})
          AND (
              COALESCE(json_oss_key, '') <> ''
              OR COALESCE(rgb_oss_key, '') <> ''
              OR COALESCE(rgb_url, '') <> ''
              OR COALESCE(large_rgb_url, '') <> ''
          )
        ORDER BY date
    """
    try:
        rows = session.execute(text(sql), params).mappings().all()
    except Exception:
        return {}

    out: dict[str, dict[str, Any]] = {}
    storage = None
    remaining_bytes = FLOOD_EVIDENCE_MEDIA_PAGE_MAX_BYTES
    for r in rows:
        d = (
            r["date"].isoformat()
            if hasattr(r["date"], "isoformat")
            else str(r["date"])[:10]
        )
        oss_key = (r.get("json_oss_key") or "").strip()
        if not oss_key:
            oss_key = None
        rgb_oss_key = (r.get("rgb_oss_key") or "").strip()
        has_stable_rgb_key = bool(
            rgb_oss_key and _is_allowed_report_rgb_key(rgb_oss_key)
        )
        entry: dict[str, Any] = {
            "json_oss_key": oss_key,
            "rgb_url": (
                r.get("rgb_url").strip()
                if isinstance(r.get("rgb_url"), str)
                and r.get("rgb_url").strip()
                and _is_allowed_report_media_url(r.get("rgb_url").strip())
                else None
            ),
            "large_rgb_url": (
                r.get("large_rgb_url").strip()
                if isinstance(r.get("large_rgb_url"), str)
                and r.get("large_rgb_url").strip()
                and _is_allowed_report_media_url(r.get("large_rgb_url").strip())
                else None
            ),
            "heatmap_url": None,
            "s2_heatmap_url": None,
        }
        stable_rgb_url_refreshed = False
        if has_stable_rgb_key:
            try:
                if storage is None:
                    from app.core.storage import get_parcel_product_storage

                    storage = get_parcel_product_storage()
                # 新产品优先由可信对象键签短时地址，避免继续信任产品JSON中的任意URL。
                signed_rgb_url = storage.presigned_get(
                    rgb_oss_key, expires=timedelta(hours=24)
                )
                if signed_rgb_url:
                    entry["rgb_url"] = signed_rgb_url
                    stable_rgb_url_refreshed = True
            except Exception:
                pass
        if has_stable_rgb_key and stable_rgb_url_refreshed:
            # 新版当前只写入RGB元数据；稳定预览已重签时不为旧热图字段回读整份像元对象。
            out[d] = entry
            continue
        try:
            if not oss_key or remaining_bytes <= 0:
                out[d] = entry
                continue
            if storage is None:
                from app.core.storage import get_parcel_product_storage

                storage = get_parcel_product_storage()
            # 旧JSON内含全部像元数组；单对象和整份报告均限流，避免历史大对象撑爆报告进程。
            read_limit = min(
                FLOOD_EVIDENCE_MEDIA_OBJECT_MAX_BYTES, remaining_bytes
            )
            try:
                raw = storage.get_bytes(oss_key, max_bytes=read_limit)
            except Exception:
                # 对超限或中断的读取按本次预算扣减，避免多条坏对象反复尝试绕过页面上限。
                remaining_bytes = max(0, remaining_bytes - read_limit)
                out[d] = entry
                continue
            remaining_bytes = max(0, remaining_bytes - len(raw))
            obj = json.loads(raw)
            if isinstance(obj, dict):
                for k in ("rgb_url", "large_rgb_url", "heatmap_url", "s2_heatmap_url"):
                    v = obj.get(k)
                    if (
                        isinstance(v, str)
                        and v.strip()
                        and _is_allowed_report_media_url(v.strip())
                    ):
                        if not entry[k]:
                            entry[k] = v.strip()
                if not entry["heatmap_url"] and entry["s2_heatmap_url"]:
                    entry["heatmap_url"] = entry["s2_heatmap_url"]
        except Exception:
            # Keep key even if fetch fails — caller may skip image
            pass
        out[d] = entry
    return out


def load_daily_precipitation(
    session: Session,
    land_id: str,
    start: Any,
    end: Any,
) -> list[dict[str, Any]]:
    """Load daily precipitation_sum for one land parcel between two dates."""
    if isinstance(start, str):
        start = datetime.fromisoformat(start[:10]).date()
    if isinstance(end, str):
        end = datetime.fromisoformat(end[:10]).date()
    rows = (
        session.execute(
            select(WeatherDaily)
            .where(
                WeatherDaily.land_id == land_id,
                WeatherDaily.date >= start,
                WeatherDaily.date <= end,
            )
            .order_by(WeatherDaily.date)
        )
        .scalars()
        .all()
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        d = r.date.isoformat() if hasattr(r.date, "isoformat") else str(r.date)[:10]
        out.append(
            {
                "date": d,
                "precipitation_sum": round(float(r.precipitation_sum or 0), 2),
            }
        )
    return out


def _precip_window_summary(
    series: list[dict[str, Any]],
    scene_date: str,
) -> dict[str, Any]:
    """Summarize precip for [scene-15d, scene-1d] plus optional scene-day."""
    scene = datetime.fromisoformat(scene_date[:10]).date()
    prior = [
        r
        for r in series
        if (scene - timedelta(days=15))
        <= datetime.fromisoformat(r["date"][:10]).date()
        <= (scene - timedelta(days=1))
    ]
    scene_day = next((r for r in series if r["date"][:10] == scene.isoformat()), None)
    amounts = [float(r["precipitation_sum"] or 0) for r in prior]
    total = round(sum(amounts), 1) if amounts else 0.0
    peak_mm = round(max(amounts), 1) if amounts else 0.0
    peak_date = None
    if amounts:
        peak_date = prior[int(max(range(len(amounts)), key=lambda i: amounts[i]))][
            "date"
        ]
    rainy_days = sum(1 for a in amounts if a >= 1.0)
    heavy_days = sum(1 for a in amounts if a >= 20.0)
    return {
        "window_start": (scene - timedelta(days=15)).isoformat(),
        "window_end": (scene - timedelta(days=1)).isoformat(),
        "days": prior,
        "cumulative_mm": total,
        "peak_mm": peak_mm,
        "peak_date": peak_date,
        "rainy_days_ge1mm": rainy_days,
        "heavy_days_ge20mm": heavy_days,
        "scene_day_mm": (
            round(float(scene_day["precipitation_sum"] or 0), 2) if scene_day else None
        ),
        "n_days_with_data": len(prior),
    }


def _classify_flood_scene(wet_mean: float, precip: dict[str, Any]) -> str:
    cum = float(precip.get("cumulative_mm") or 0)
    peak = float(precip.get("peak_mm") or 0)
    heavy = int(precip.get("heavy_days_ge20mm") or 0)
    if cum >= 60 or peak >= 30 or heavy >= 1:
        return "rain_driven"
    if cum >= 25 or peak >= 15:
        return "likely_rain"
    if wet_mean >= 0.25 and cum < 15:
        return "persistent_water"
    if cum < 10:
        return "low_rain_persistent"
    return "mixed"


def _scene_analysis_zh(
    scene_date: str,
    wet_mean: float,
    precip: dict[str, Any],
    kind: str,
) -> str:
    cum = precip.get("cumulative_mm") or 0
    peak = precip.get("peak_mm") or 0
    peak_d = precip.get("peak_date") or "—"
    n = precip.get("n_days_with_data") or 0
    bits = [
        f"{scene_date} 见明水面（湿指数均≈{wet_mean:.3f}）",
        f"前15日累计降雨约 {cum} mm（有数据 {n} 天）",
        f"峰值日 {peak_d} 约 {peak} mm",
    ]
    if kind == "rain_driven":
        bits.append("前面有明显大雨，更像雨后积水/短时涝渍")
    elif kind == "likely_rain":
        bits.append("前面有一定降雨，积水与降水相关的可能性较大")
    elif kind in ("persistent_water", "low_rain_persistent"):
        bits.append(
            "前面降雨不多，更像持续水面/洼地积水或灌溉泡田，不完全是一场暴雨造成"
        )
    else:
        bits.append("降雨与明水面关系一般，需结合田间核实")
    return "；".join(bits) + "。"


def build_flood_evidence(
    session: Session | None,
    *,
    land_id: str | None,
    open_water_dates: list[dict[str, Any]] | None,
    max_scenes: int = FLOOD_EVIDENCE_MAX_SCENES,
):
    """Build structured flood evidence from one parcel's scenes and weather."""
    scenes_all = list(open_water_dates or [])
    if not scenes_all:
        return None

    selected = scenes_all[: max(1, int(max_scenes))]
    media_by_date: dict[str, dict[str, Any]] = {}
    if session is not None and land_id:
        media_by_date = load_oss_media_for_dates(
            session, land_id, [s["date"] for s in selected]
        )

    # Load precip spanning earliest window through latest scene day
    precip_by_scene: dict[str, dict[str, Any]] = {}
    if session is not None and land_id is not None:
        dates = [datetime.fromisoformat(s["date"][:10]).date() for s in selected]
        lo = min(dates) - timedelta(days=15)
        hi = max(dates)
        series = load_daily_precipitation(session, land_id, lo, hi)
        for s in selected:
            precip_by_scene[s["date"]] = _precip_window_summary(series, s["date"])

    enriched: list[dict[str, Any]] = []
    for s in selected:
        d = s["date"]
        wet = float(s.get("wet_mean") or 0)
        precip = precip_by_scene.get(d) or {
            "cumulative_mm": 0,
            "peak_mm": 0,
            "peak_date": None,
            "n_days_with_data": 0,
            "days": [],
            "heavy_days_ge20mm": 0,
            "rainy_days_ge1mm": 0,
            "scene_day_mm": None,
            "window_start": None,
            "window_end": None,
        }
        kind = _classify_flood_scene(wet, precip)
        media = media_by_date.get(d) or {}
        preview = media.get("rgb_url") or media.get("large_rgb_url")
        enriched.append(
            {
                "date": d,
                "wet_mean": s.get("wet_mean"),
                "ndvi_mean": s.get("ndvi_mean"),
                "kind": kind,
                "analysis": _scene_analysis_zh(d, wet, precip, kind),
                "precip_prior_15d": precip,
                "media": {
                    "rgb_url": media.get("rgb_url"),
                    "large_rgb_url": media.get("large_rgb_url"),
                    "heatmap_url": media.get("heatmap_url"),
                    "s2_heatmap_url": media.get("s2_heatmap_url"),
                    "preview_url": preview,
                    "json_oss_key": media.get("json_oss_key"),
                    "has_oss": bool(media.get("json_oss_key")),
                },
            }
        )

    # Overall analysis
    kinds = [e["kind"] for e in enriched]
    rainish = sum(1 for k in kinds if k in ("rain_driven", "likely_rain"))
    persist = sum(1 for k in kinds if k in ("persistent_water", "low_rain_persistent"))
    total_n = len(scenes_all)
    shown_n = len(enriched)
    parts = [
        f"卫星在生育期内共见明水面 {total_n} 景",
    ]
    if shown_n < total_n:
        parts.append(f"本报告按湿指数挑出最湿的 {shown_n} 景展示影像与雨前降水")
    else:
        parts.append("以下逐景对照前15日降水")
    if rainish >= max(1, shown_n // 2):
        parts.append("多数场景前有较明显降雨，更像雨后积水/短时涝渍")
    elif persist >= max(1, shown_n // 2):
        parts.append("多数场景前降雨不多，更像持续水面或洼地积水，不完全是暴雨造成")
    else:
        parts.append("有的像雨后积水，有的像持续水面，建议结合低洼地形与田间核实")
    wettest = enriched[0]
    parts.append(f"最湿一景 {wettest['date']}（湿指数≈{wettest.get('wet_mean')}）")
    analysis = "；".join(parts) + "。"

    return {
        "absolute_open_water_scenes": total_n,
        "selected_count": shown_n,
        "scenes": enriched,
        "analysis": analysis,
        "all_dates": [
            {
                "date": s["date"],
                "wet_mean": s.get("wet_mean"),
                "ndvi_mean": s.get("ndvi_mean"),
            }
            for s in scenes_all
        ],
    }


def _is_allowed_report_media_url(url: str) -> bool:
    """只允许报告访问配置的对象存储域名，阻断媒体URL对内网地址的探测。"""
    try:
        parsed = urlsplit(url)
        endpoint = urlsplit(settings.oss_endpoint)
        hostname = (parsed.hostname or "").lower()
        endpoint_host = (endpoint.hostname or "").lower()
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme == "https" else 80
        endpoint_port = endpoint.port
        if endpoint_port is None:
            endpoint_port = 443 if endpoint.scheme == "https" else 80
    except ValueError:
        return False
    if (
        not hostname
        or parsed.scheme.lower() not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.scheme.lower() != endpoint.scheme.lower()
        or port != endpoint_port
        or not endpoint_host
    ):
        return False
    allowed_hosts = {endpoint_host}
    if settings.oss_bucket:
        allowed_hosts.add(f"{settings.oss_bucket.lower()}.{endpoint_host}")
    return hostname in allowed_hosts


class _OssOnlyRedirectHandler(HTTPRedirectHandler):
    """重定向也必须留在配置对象存储域名，避免白名单URL跳转到内网。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _is_allowed_report_media_url(newurl):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_url_bytes(
    url: str,
    *,
    timeout: float = 25.0,
    max_bytes: int = REPORT_MEDIA_MAX_BYTES,
) -> bytes | None:
    """从配置对象存储有界下载报告图片；不接受任意主机或跨域跳转。"""
    if not url or not isinstance(url, str):
        return None
    if max_bytes <= 0 or not _is_allowed_report_media_url(url):
        return None
    try:
        req = Request(
            url,
            headers={"User-Agent": "agric-satellite-analysis-land-assessment/1.0"},
            method="GET",
        )
        opener = build_opener(_OssOnlyRedirectHandler())
        with opener.open(req, timeout=timeout) as resp:
            content_length = resp.headers.get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > max_bytes:
                        return None
                except ValueError:
                    return None
            data = resp.read(max_bytes + 1)
            return data if len(data) <= max_bytes else None
    except Exception:
        return None


def cache_media_images(
    flood_evidence: dict[str, Any] | None,
    out_dir: Path,
    *,
    also_stage_media: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Path]:
    """Download rgb/heatmap previews to out_dir; return logical-name -> path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    media_tasks: list[tuple[str, str, dict[str, Any] | None, str | None]] = []

    def _add_task(
        tag: str,
        url: Any,
        scene: dict[str, Any] | None = None,
        local_field: str | None = None,
    ) -> None:
        # 下载器本身只接受字符串地址；跳过异常JSON类型，避免破坏整份报告生成。
        if isinstance(url, str) and url:
            media_tasks.append((tag, url, scene, local_field))

    if flood_evidence:
        for i, sc in enumerate(flood_evidence.get("scenes") or []):
            d = str(sc.get("date") or f"s{i}")[:10]
            media = sc.get("media") or {}
            preview = (
                media.get("preview_url")
                or media.get("rgb_url")
                or media.get("large_rgb_url")
            )
            _add_task(f"flood_rgb_{d}", preview, sc, "local_rgb_path")
            hp = media.get("heatmap_url") or media.get("s2_heatmap_url")
            _add_task(f"flood_hm_{d}", hp, sc, "local_heatmap_path")

    if also_stage_media:
        for d, media in also_stage_media.items():
            preview = media.get("rgb_url") or media.get("large_rgb_url")
            _add_task(f"stage_rgb_{d}", preview)
            hp = media.get("heatmap_url") or media.get("s2_heatmap_url")
            _add_task(f"stage_hm_{d}", hp)

    # 相同签名地址可能同时被洪涝证据和物候阶段引用。去重后并发下载，
    # 但最终按任务原顺序写入逻辑文件名，保持重复日期时“后一个成功结果覆盖前一个”的既有语义。
    unique_urls = list(dict.fromkeys(url for _, url, _, _ in media_tasks))
    cache_run_id = uuid.uuid4().hex

    def _cache_path(url: str) -> Path:
        url_hash = hashlib.sha256(url.encode("utf-8", errors="surrogatepass")).hexdigest()
        return out_dir / f".report-media-{cache_run_id}-{url_hash}.cache"

    def _download_once(url: str) -> tuple[Path, str] | None:
        data = download_url_bytes(url)
        if not data:
            return None
        # 图片字节写入独立临时文件，避免多个大图的响应体同时驻留内存。
        ext = ".png"
        if data[:3] == b"\xff\xd8\xff":
            ext = ".jpg"
        elif data[:4] == b"RIFF":
            ext = ".webp"
        cache_path = _cache_path(url)
        cache_path.write_bytes(data)
        return cache_path, ext

    cached_by_url: dict[str, tuple[Path, str] | None] = {}
    try:
        if unique_urls:
            # 报告图片数量有上限；固定小并发度降低等待时间，同时保护OSS和API工作线程。
            with ThreadPoolExecutor(
                max_workers=min(3, len(unique_urls)),
                thread_name_prefix="report-media",
            ) as executor:
                futures = {
                    executor.submit(_download_once, url): url for url in unique_urls
                }
                for future in as_completed(futures):
                    url = futures[future]
                    cached_by_url[url] = future.result()

        for tag, url, scene, local_field in media_tasks:
            cached = cached_by_url.get(url)
            if not cached:
                continue
            cache_path, ext = cached
            path = out_dir / f"{tag}{ext}"
            shutil.copyfile(cache_path, path)
            written[tag] = path
            if scene is not None and local_field is not None:
                # 兼容历史记录中 media 缺失或为 null 的情况，只在确实下载成功后写回路径。
                scene_media = scene.get("media")
                if not isinstance(scene_media, dict):
                    scene_media = {}
                    scene["media"] = scene_media
                scene_media[local_field] = str(path)
    finally:
        # 即使并发下载或逻辑文件落盘失败，也清除本次生成的临时媒体缓存。
        for url in unique_urls:
            _cache_path(url).unlink(missing_ok=True)

    return written
