# -*- coding: utf-8 -*-
"""Deterministic season-growth facts from agri / field_stats."""

from __future__ import annotations

import logging
import re
import uuid
from collections import Counter
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.core.agri_classify import (
    CLOUD_MAX_PCT,
    OpticalObs,
    SarObs,
    classify_drought_series,
    classify_flood_series,
    cloud_pct,
    is_drought_day_class,
    is_official_optical_product,
)
from app.core.agri_tags import parse_agri_land_id
from app.core.harvest_detect import detect_harvest
from openfarm_common.growing_seasons import months_from_window

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def _iso(d: Any) -> str:
    if hasattr(d, "isoformat"):
        return d.isoformat()[:10]
    return str(d)[:10]


def _parse_date(value: str | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _in_window(d: str, start: date, end: date) -> bool:
    dd = _parse_date(d)
    return dd is not None and start <= dd <= end


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _series_mean(points: list[dict[str, Any]], key: str = "value") -> float | None:
    vals = [_num(p.get(key)) for p in points]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _peak(points: list[dict[str, Any]]) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_v = float("-inf")
    for p in points:
        v = _num(p.get("value"))
        if v is None:
            continue
        if v > best_v:
            best_v = v
            best = {"date": p.get("date"), "value": round(v, 4)}
    return best


def load_agri_s2_rows(
    session: "Session",
    land_id: str,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    from sqlalchemy import text

    rows = (
        session.execute(
            text(
                f"""
            SELECT date, scene_id, ndvi_avg, evi_avg, mndwi_avg, ndmi_avg,
                   parcel_cloud_cover_pct, cloud_cover,
                   pixel_data->>'source' AS source,
                   pixel_data->>'decloud_quality' AS decloud_quality,
                   rgb_url, large_rgb_url, rgb_oss_key,
                   pixel_data->>'format' AS pixel_format,
                   CASE
                     WHEN jsonb_typeof(pixel_data->'pixels') = 'array'
                     THEN jsonb_array_length(pixel_data->'pixels')
                     ELSE 0
                   END AS pixel_n
            FROM agri.parcel_scene_products
            WHERE land_id = :land_id AND sensor = 'S2'
              AND date >= :start_date AND date <= :end_date
            ORDER BY date
            """
            ),
            {
                "land_id": land_id,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            },
        )
        .mappings()
        .all()
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        d = _iso(r["date"])
        cloud = cloud_pct(r["parcel_cloud_cover_pct"], r["cloud_cover"])
        official = is_official_optical_product(
            source=r.get("source"),
            scene_id=r.get("scene_id") or r.get("product_id"),
            parcel_cloud_cover_pct=r["parcel_cloud_cover_pct"],
            cloud_cover=r["cloud_cover"],
            decloud_quality=r.get("decloud_quality"),
            cloud_max_pct=CLOUD_MAX_PCT,
        )
        out.append(
            {
                "date": d,
                "scene_id": r.get("scene_id") or r.get("product_id"),
                "ndvi_avg": _num(r["ndvi_avg"]),
                "evi_avg": _num(r["evi_avg"]),
                "ndmi_avg": _num(r["ndmi_avg"]),
                "mndwi_avg": _num(r["mndwi_avg"]),
                "parcel_cloud_cover_pct": _num(r["parcel_cloud_cover_pct"]),
                "cloud_cover": _num(r["cloud_cover"]),
                "cloud_pct": cloud,
                "decloud_quality": r.get("decloud_quality"),
                "source": r.get("source"),
                "official": bool(official),
                "clear": cloud is not None and cloud <= CLOUD_MAX_PCT,
                "rgb_url": (r.get("rgb_url") or None) or None,
                "large_rgb_url": (r.get("large_rgb_url") or None) or None,
                "rgb_oss_key": (r.get("rgb_oss_key") or None) or None,
                "pixel_format": r.get("pixel_format"),
                "pixel_n": int(r["pixel_n"] or 0) if r.get("pixel_n") is not None else 0,
            }
        )
        if out[-1]["rgb_url"] == "":
            out[-1]["rgb_url"] = None
        if out[-1]["large_rgb_url"] == "":
            out[-1]["large_rgb_url"] = None
        if out[-1]["rgb_oss_key"] == "":
            out[-1]["rgb_oss_key"] = None
    return out


def load_agri_s1_rows(
    session: "Session",
    land_id: str,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    from sqlalchemy import text
    from app.core.agri_classify import parse_s1_relative_orbit

    rows = (
        session.execute(
            text(
                """
            SELECT date, scene_id, vv_avg, vh_avg,
                   NULLIF(pixel_data->>'relative_orbit', '')::int AS relative_orbit
            FROM agri.parcel_scene_products
            WHERE land_id = :land_id AND sensor = 'S1'
              AND date >= :start_date AND date <= :end_date
            ORDER BY date
            """
            ),
            {
                "land_id": land_id,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            },
        )
        .mappings()
        .all()
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        sid = r.get("scene_id") or r.get("product_id")
        rel = r.get("relative_orbit")
        if rel is None:
            rel = parse_s1_relative_orbit(sid)
        out.append(
            {
                "date": _iso(r["date"]),
                "scene_id": sid,
                "vv_avg": _num(r["vv_avg"]),
                "vh_avg": _num(r["vh_avg"]),
                "relative_orbit": rel,
            }
        )
    return out


def _indices_to_ndvi_ndmi(
    indices: list[dict[str, Any]], start: date, end: date
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_date: dict[str, dict[str, float]] = {}
    for row in indices:
        d = _iso(row.get("date"))
        if not _in_window(d, start, end):
            continue
        layer = str(row.get("layer_type") or "").upper()
        mean = _num(row.get("mean"))
        if mean is None:
            continue
        by_date.setdefault(d, {})[layer] = mean
    ndvi = [
        {"date": d, "value": round(vals["NDVI"], 4)}
        for d, vals in sorted(by_date.items())
        if "NDVI" in vals
    ]
    ndmi = [
        {"date": d, "value": round(vals["NDMI"], 4)}
        for d, vals in sorted(by_date.items())
        if "NDMI" in vals
    ]
    return ndvi, ndmi


def load_classic_indices(
    session: "Session", field_id: uuid.UUID, start: date, end: date
) -> list[dict[str, Any]]:
    from sqlalchemy import select
    from app.models.tables import FieldStat, RasterLayer

    rows = session.execute(
        select(
            FieldStat.date,
            RasterLayer.layer_type,
            FieldStat.mean,
        )
        .join(RasterLayer, RasterLayer.id == FieldStat.layer_id)
        .where(
            FieldStat.field_id == field_id,
            FieldStat.date >= start,
            FieldStat.date <= end,
            RasterLayer.layer_type.in_(("NDVI", "NDMI", "EVI")),
        )
        .order_by(FieldStat.date)
    ).all()
    return [
        {
            "date": _iso(r.date),
            "layer_type": r.layer_type,
            "mean": _num(r.mean),
        }
        for r in rows
    ]


def _drought_summary(
    s2_rows: list[dict[str, Any]], season_months: tuple[int, ...] | list[int]
) -> dict[str, Any]:
    observations: list[OpticalObs] = []
    for r in s2_rows:
        observations.append(
            {
                "date": r["date"],
                "ndvi": r.get("ndvi_avg"),
                "ndmi": r.get("ndmi_avg"),
                "official": bool(r.get("official")),
                "scene_id": r.get("scene_id"),
                "decloud_quality": r.get("decloud_quality"),
                "cloud_cover": r.get("cloud_cover"),
                "parcel_cloud_cover_pct": r.get("parcel_cloud_cover_pct"),
            }
        )
    classified = classify_drought_series(
        observations, season_months=season_months or (6, 7, 8, 9)
    )
    counts: Counter[str] = Counter()
    days: list[dict[str, str]] = []
    scene_classes: list[dict[str, str]] = []
    for d, cls in classified:
        c = str(cls)
        counts[c] += 1
        scene_classes.append({"date": d, "class": c})
        if is_drought_day_class(cls):
            days.append({"date": d, "class": c})
    usable = dedupe_usable_scene_classes(scene_classes)
    usable_counts: Counter[str] = Counter()
    usable_days: list[dict[str, str]] = []
    for sc in usable:
        c = str(sc.get("class") or "")
        usable_counts[c] += 1
        if is_drought_day_class(c):
            usable_days.append({"date": sc["date"], "class": c})
    return {
        # Prefer deduped official/usable counts for report cards / timeline.
        "counts": dict(usable_counts),
        "raw_counts": dict(counts),
        "drought_scene_count": sum(
            usable_counts.get(k, 0) for k in ("mild", "moderate", "severe")
        ),
        "days": usable_days[:40],
        "scene_classes": scene_classes,
        "usable_scene_classes": usable,
        "classified": scene_classes,
    }


def _flood_summary(s1_rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not s1_rows:
        return {
            "status": "no_s1_data",
            "scene_count": 0,
            "vv_median": None,
            "counts": {},
            "scenes": [],
            "note": "窗口内无 Sentinel-1 数据，无法做洪涝判定",
        }
    observations: list[SarObs] = []
    vvs: list[float] = []
    for r in s1_rows:
        vv = r.get("vv_avg")
        if vv is not None:
            vvs.append(float(vv))
        observations.append(
            {
                "date": r["date"],
                "vv": r.get("vv_avg"),
                "vh": r.get("vh_avg"),
                "scene_id": r.get("scene_id"),
                "relative_orbit": r.get("relative_orbit"),
            }
        )
    classified = classify_flood_series(observations)
    counts: Counter[str] = Counter()
    scenes: list[dict[str, Any]] = []
    for i, (d, cls) in enumerate(classified):
        if cls is None:
            counts["unknown"] += 1
            c: str | None = None
        else:
            c = str(cls)
            counts[c] += 1
        row = s1_rows[i] if i < len(s1_rows) else {}
        scenes.append(
            {
                "date": d,
                "vv": row.get("vv_avg"),
                "vh": row.get("vh_avg"),
                "relative_orbit": row.get("relative_orbit"),
                "class": c,
            }
        )
    vvs_sorted = sorted(vvs)
    vv_median = None
    if vvs_sorted:
        mid = len(vvs_sorted) // 2
        if len(vvs_sorted) % 2:
            vv_median = round(vvs_sorted[mid], 3)
        else:
            vv_median = round((vvs_sorted[mid - 1] + vvs_sorted[mid]) / 2, 3)
    flood_n = counts.get("flood_moderate", 0) + counts.get("flood_severe", 0)
    return {
        "status": "ok",
        "scene_count": len(s1_rows),
        "vv_median": vv_median,
        "counts": dict(counts),
        "flood_scene_count": flood_n,
        "scenes": scenes,
        "note": None,
    }


def _prior_year_comparison(
    session: "Session",
    *,
    land_id: str | None,
    field_id: uuid.UUID | None,
    start: date,
    end: date,
    is_agri: bool,
) -> dict[str, Any] | None:
    try:
        prior_start = date(start.year - 1, start.month, start.day)
        prior_end = date(end.year - 1, end.month, end.day)
    except ValueError:
        # Feb 29 etc.
        prior_start = start - timedelta(days=365)
        prior_end = end - timedelta(days=365)

    if is_agri and land_id:
        rows = load_agri_s2_rows(session, land_id, prior_start, prior_end)
        ndvi = [
            {"date": r["date"], "value": r["ndvi_avg"]}
            for r in rows
            if r.get("ndvi_avg") is not None
        ]
    elif field_id:
        indices = load_classic_indices(session, field_id, prior_start, prior_end)
        ndvi, _ = _indices_to_ndvi_ndmi(indices, prior_start, prior_end)
    else:
        return None
    if len(ndvi) < 2:
        return None
    official_count = None
    if is_agri and land_id:
        official_count = sum(1 for r in rows if r.get("official"))
    return {
        "start_date": prior_start.isoformat(),
        "end_date": prior_end.isoformat(),
        "ndvi_mean": round(_series_mean(ndvi) or 0.0, 4)
        if _series_mean(ndvi) is not None
        else None,
        "ndvi_peak": _peak(ndvi),
        "point_count": len(ndvi),
        "official_count": official_count,
    }



DROUGHT_CLASS_CN = {
    "severe": "重度",
    "moderate": "中度",
    "mild": "轻度",
    "normal": "正常",
    "unreliable": "不可靠",
    "out_of_season": "季外",
}

FLOOD_CLASS_CN = {
    "flood_severe": "洪涝(重)",
    "flood_moderate": "洪涝",
    "watch": "关注",
    "dry": "正常",
}

QUALITY_CN = {
    "official": "官方",
    "good": "良好",
    "fair": "一般",
    "bad": "较差",
    "raw": "原始",
    "classic": "经典",
}


def drought_class_cn(cls: str | None) -> str:
    if cls is None:
        return "缺测/未定"
    return DROUGHT_CLASS_CN.get(str(cls), str(cls))


def flood_class_cn(cls: str | None) -> str:
    if cls is None:
        return "缺测/未定"
    return FLOOD_CLASS_CN.get(str(cls), str(cls))


def quality_cn(quality: str | None) -> str:
    if quality is None or quality == "":
        return "—"
    q = str(quality).strip().lower()
    return QUALITY_CN.get(q, str(quality))


def _methodology() -> dict[str, Any]:
    """Short Chinese summary of drought / flood rules (mirrors agri_classify)."""
    return {
        "drought": (
            "Sentinel-2 光学干旱：仅官方可用景（晴空或 good 去云）且落在生育期月份参与判定；"
            "以 NDDI=(NDVI-NDMI)/(NDVI+NDMI) 为主，结合同月 NDDI 分位及 NDMI/NDVI 相对同月中位数的下降；"
            "轻度/中度/重度对应 NDDI 阈值与绿度跌幅；季外标「季外」，非官方标「不可靠」。"
        ),
                "flood": (
            "Sentinel-1 洪涝：按相对轨道建 VV 基线；"
            "洪涝需同时满足 VV≤-17.0 dB、相对基线下降≥3.0 dB，且 VH 或 VV-VH 辅助条件；"
            "VV≤-15.0 dB 的近阈值情形标「关注」；仅 VV-VH 不会单独判洪涝。"
        ),
        "sensors": "Sentinel-2（光学 NDVI/NDMI/EVI/MNDWI）与 Sentinel-1（SAR VV/VH）。",
    }


def _build_s2_appendix(
    s2_rows: list[dict[str, Any]], drought: dict[str, Any]
) -> list[dict[str, Any]]:
    class_by_date: dict[str, str] = {}
    for sc in drought.get("scene_classes") or drought.get("classified") or []:
        d = sc.get("date")
        if d and d not in class_by_date:
            class_by_date[str(d)] = str(sc.get("class") or "")
    out: list[dict[str, Any]] = []
    for r in s2_rows:
        d = str(r.get("date") or "")
        cls = class_by_date.get(d)
        q = r.get("decloud_quality") or ("official" if r.get("official") else "raw")
        out.append(
            {
                "date": d,
                "cloud_pct": r.get("cloud_pct"),
                "quality": q,
                "quality_cn": quality_cn(q),
                "drought_class": cls,
                "drought_class_cn": drought_class_cn(cls),
                "ndvi": r.get("ndvi_avg"),
                "ndmi": r.get("ndmi_avg"),
                "evi": r.get("evi_avg"),
                "mndwi": r.get("mndwi_avg"),
            }
        )
    return out


def _build_s1_appendix(flood: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sc in flood.get("scenes") or []:
        cls = sc.get("class")
        out.append(
            {
                "date": sc.get("date"),
                "relative_orbit": sc.get("relative_orbit"),
                "vv": sc.get("vv"),
                "vh": sc.get("vh"),
                "flood_class": cls,
                "flood_class_cn": flood_class_cn(cls),
            }
        )
    return out



# Prefer usable drought classes over unreliable when collapsing raw+good duplicates.
_DROUGHT_CLASS_PRIORITY = {
    "severe": 60,
    "moderate": 50,
    "mild": 40,
    "normal": 30,
    "out_of_season": 20,
    "unreliable": 10,
}


def dedupe_usable_scene_classes(
    scene_classes: list[dict[str, Any]] | None,
) -> list[dict[str, str]]:
    """One class per date from official/usable scenes (not raw+good duplicates).

    When the same date appears multiple times (e.g. raw unreliable + good severe),
    keep the higher-priority usable class so monthly moisture matches drought.days.
    """
    by_date: dict[str, dict[str, str]] = {}
    for sc in scene_classes or []:
        d = str(sc.get("date") or "")[:10]
        if not d:
            continue
        cls = str(sc.get("class") or "")
        prev = by_date.get(d)
        if prev is None:
            by_date[d] = {"date": d, "class": cls}
            continue
        prev_cls = str(prev.get("class") or "")
        if _DROUGHT_CLASS_PRIORITY.get(cls, 0) > _DROUGHT_CLASS_PRIORITY.get(prev_cls, 0):
            by_date[d] = {"date": d, "class": cls}
    return [by_date[k] for k in sorted(by_date.keys())]


def count_classes_by_month(
    usable: list[dict[str, Any]], prefix: str
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for sc in usable:
        if not str(sc.get("date") or "").startswith(prefix):
            continue
        c = str(sc.get("class") or "")
        if not c:
            continue
        counts[c] = counts.get(c, 0) + 1
    return counts


def drought_days_counts_for_month(
    days: list[dict[str, Any]] | None, prefix: str
) -> dict[str, int]:
    """Unique-date drought-day counts for a YYYY-MM prefix."""
    by_date: dict[str, str] = {}
    for d in days or []:
        ds = str(d.get("date") or "")[:10]
        if not ds.startswith(prefix):
            continue
        cls = str(d.get("class") or "")
        if not is_drought_day_class(cls):
            continue
        prev = by_date.get(ds)
        if prev is None or _DROUGHT_CLASS_PRIORITY.get(cls, 0) > _DROUGHT_CLASS_PRIORITY.get(
            prev, 0
        ):
            by_date[ds] = cls
    out: dict[str, int] = {}
    for cls in by_date.values():
        out[cls] = out.get(cls, 0) + 1
    return out


def ensure_timeline_moisture_consistent(
    timeline: list[dict[str, Any]],
    drought: dict[str, Any],
) -> list[dict[str, Any]]:
    """If monthly moisture drought counts ≠ drought.days, log and prefer scene re-agg."""
    usable = list(drought.get("usable_scene_classes") or [])
    if not usable:
        usable = dedupe_usable_scene_classes(
            drought.get("scene_classes") or drought.get("classified") or []
        )
    days = list(drought.get("days") or [])
    out: list[dict[str, Any]] = []
    for row in timeline:
        row = dict(row)
        prefix = str(row.get("month") or "")
        if not prefix:
            out.append(row)
            continue
        scene_counts = count_classes_by_month(usable, prefix)
        day_counts = drought_days_counts_for_month(days, prefix)
        scene_drought = {
            k: scene_counts.get(k, 0) for k in ("severe", "moderate", "mild")
        }
        # Compare drought-day subset only
        mismatch = any(
            int(scene_drought.get(k) or 0) != int(day_counts.get(k) or 0)
            for k in ("severe", "moderate", "mild")
        )
        if mismatch:
            logger.warning(
                "timeline moisture inconsistency month=%s scene=%s days=%s; preferring scene re-aggregation",
                prefix,
                scene_drought,
                day_counts,
            )
            # Prefer scene re-aggregation: rebuild moisture from usable scenes
            row["moisture"] = format_drought_counts_inline(scene_counts)
            row["moisture_counts"] = scene_counts
            row["drought_days"] = sum(scene_drought.values())
            row["moisture_source"] = "usable_scene_reagg"
        else:
            row["moisture"] = format_drought_counts_inline(scene_counts)
            row["moisture_counts"] = scene_counts
            row["drought_days"] = sum(scene_drought.values())
            row["moisture_source"] = "usable_scene"
        out.append(row)
    return out


def _build_timeline(
    *,
    start: date,
    end: date,
    s2_rows: list[dict[str, Any]],
    s1_rows: list[dict[str, Any]],
    drought: dict[str, Any],
    flood: dict[str, Any],
    ndvi_ts: list[dict[str, Any]] | None = None,
    crops: list[Any] | None = None,
    fallback_crop: str | None = None,
    peak_month: int | None = None,
) -> list[dict[str, Any]]:
    """Month rows: program scene / drought / flood / estimated phenology."""
    months: list[tuple[int, int]] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append((y, m))
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1

    usable_classes = list(drought.get("usable_scene_classes") or [])
    if not usable_classes:
        usable_classes = dedupe_usable_scene_classes(
            drought.get("scene_classes") or drought.get("classified") or []
        )
    drought_days = {
        str(d.get("date")): str(d.get("class"))
        for d in (drought.get("days") or [])
    }
    class_by_date = {str(sc.get("date")): str(sc.get("class") or "") for sc in usable_classes}
    flood_scenes = list(flood.get("scenes") or [])
    ndvi_ts = list(ndvi_ts or [])

    rows: list[dict[str, Any]] = []
    for yy, mm in months:
        prefix = f"{yy:04d}-{mm:02d}"
        s2_n = sum(1 for r in s2_rows if str(r.get("date", "")).startswith(prefix))
        official_n = sum(
            1
            for r in s2_rows
            if str(r.get("date", "")).startswith(prefix) and r.get("official")
        )
        if not official_n and ndvi_ts:
            official_n = sum(
                1
                for p in ndvi_ts
                if str(p.get("date", "")).startswith(prefix)
                and p.get("official") is not False
            )
        s1_n = sum(1 for r in s1_rows if str(r.get("date", "")).startswith(prefix))
        drought_n = sum(1 for d in drought_days if d.startswith(prefix))
        flood_n = sum(
            1
            for sc in flood_scenes
            if str(sc.get("date", "")).startswith(prefix)
            and sc.get("class") in ("flood_moderate", "flood_severe")
        )
        watch_n = sum(
            1
            for sc in flood_scenes
            if str(sc.get("date", "")).startswith(prefix) and sc.get("class") == "watch"
        )
        month_ndvi = [
            _num(p.get("value"))
            for p in ndvi_ts
            if str(p.get("date", "")).startswith(prefix)
            and p.get("official") is not False
            and _num(p.get("value")) is not None
        ]
        month_ndvi_vals = [v for v in month_ndvi if v is not None]
        ndvi_mean = (
            round(sum(month_ndvi_vals) / len(month_ndvi_vals), 4)
            if month_ndvi_vals
            else None
        )
        ndvi_max = round(max(month_ndvi_vals), 4) if month_ndvi_vals else None
        month_drought_counts = count_classes_by_month(usable_classes, prefix)
        if month_ndvi_vals:
            s2_growth = (
                f"官方/可用{official_n or len(month_ndvi_vals)}景，"
                f"NDVI均{_fmt_idx(ndvi_mean, 3)}，"
                f"最高{_fmt_idx(ndvi_max, 3)}"
            )
        else:
            s2_growth = f"S2 {s2_n} 景，可用绿度点不足"
        moisture = format_drought_counts_inline(month_drought_counts)
        day_counts = drought_days_counts_for_month(
            list(drought.get("days") or []), prefix
        )
        scene_drought_n = sum(
            int(month_drought_counts.get(k) or 0) for k in ("mild", "moderate", "severe")
        )
        days_drought_n = sum(int(day_counts.get(k) or 0) for k in ("mild", "moderate", "severe"))
        if scene_drought_n != days_drought_n:
            logger.warning(
                "timeline moisture inconsistency month=%s scene_drought=%s days=%s; preferring scene re-aggregation",
                prefix,
                {k: month_drought_counts.get(k, 0) for k in ("severe", "moderate", "mild")},
                day_counts,
            )
            drought_n = scene_drought_n
        else:
            drought_n = scene_drought_n
        if flood_n:
            s1_flood = f"洪涝{flood_n}" + (f" / 关注{watch_n}" if watch_n else "")
        elif watch_n:
            s1_flood = f"关注{watch_n} / 未检出洪涝"
        elif s1_n:
            s1_flood = f"未检出洪涝（{s1_n}景）"
        else:
            s1_flood = "无S1"
        rows.append(
            {
                "month": prefix,
                "period_label": f"{mm}月",
                "crop_stage_estimate": phenology_stage_estimate(
                    crops, mm, peak_month=peak_month, fallback_crop=fallback_crop
                ),
                "s2_growth": s2_growth,
                "moisture": moisture,
                "moisture_counts": month_drought_counts,
                "s1_flood": s1_flood,
                "s2_count": s2_n,
                "s2_official_count": official_n,
                "s1_count": s1_n,
                "drought_days": drought_n,
                "flood_count": flood_n,
                "watch_count": watch_n,
                "ndvi_mean": ndvi_mean,
                "ndvi_max": ndvi_max,
            }
        )
    return rows



# --- layout v2: program-owned status / confidence / phenology / YoY ---

CONF_CN = {"high": "高", "medium": "中", "low": "低"}

# Typical calendar stages. Always suffixed 估计; NEVER a real sowing date.
_CROP_STAGE_BY_MONTH: dict[str, dict[int, str]] = {
    "corn": {
        5: "出苗（估计）",
        6: "苗期–拔节（估计）",
        7: "拔节–抽雄/吐丝（估计）",
        8: "灌浆（估计）",
        9: "成熟（估计）",
        10: "收获后残茬（估计）",
    },
    "wheat": {
        3: "返青–拔节（估计）",
        4: "拔节–抽穗（估计）",
        5: "抽穗–灌浆（估计）",
        6: "成熟（估计）",
        7: "收获后（估计）",
    },
    "rice": {
        5: "移栽–返青（估计）",
        6: "返青–分蘖（估计）",
        7: "拔节–抽穗（估计）",
        8: "灌浆（估计）",
        9: "成熟（估计）",
    },
    "soybean": {
        6: "苗期–分枝（估计）",
        7: "开花–结荚（估计）",
        8: "鼓粒（估计）",
        9: "成熟（估计）",
    },
}

_CROP_ALIASES = {
    "玉米": "corn",
    "corn": "corn",
    "maize": "corn",
    "summer_corn": "corn",
    "summer-corn": "corn",
    "夏玉米": "corn",
    "春玉米": "corn",
    "wheat": "wheat",
    "小麦": "wheat",
    "冬小麦": "wheat",
    "rice": "rice",
    "水稻": "rice",
    "soybean": "soybean",
    "大豆": "soybean",
}


def normalize_crop_key(crops: list[Any] | None, fallback: str | None = None) -> str:
    tokens: list[str] = []
    for c in crops or []:
        tokens.append(str(c).strip())
    if fallback:
        tokens.append(str(fallback).strip())
    for t in tokens:
        key = _CROP_ALIASES.get(t) or _CROP_ALIASES.get(t.lower())
        if key:
            return key
        low = t.lower()
        for alias, mapped in _CROP_ALIASES.items():
            if alias in t or alias in low:
                return mapped
    return "generic"


def phenology_stage_estimate(
    crops: list[Any] | None,
    month: int,
    *,
    peak_month: int | None = None,
    fallback_crop: str | None = None,
) -> str:
    """Calendar-typical stage label. Always 估计; no sowing date."""
    crop = normalize_crop_key(crops, fallback_crop)
    table = _CROP_STAGE_BY_MONTH.get(crop) or {}
    if month in table:
        return table[month]
    if peak_month:
        if month < peak_month:
            return "营养生长（估计）"
        if month == peak_month:
            return "旺盛生长期（估计）"
        return "成熟/衰老（估计）"
    return "生育阶段（估计）"


def phenology_bands(
    start: date,
    end: date,
    crops: list[Any] | None,
    *,
    peak_month: int | None = None,
    fallback_crop: str | None = None,
) -> list[dict[str, Any]]:
    """Month bands for charts / timeline. Labels include 估计."""
    bands: list[dict[str, Any]] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        last_day = 28
        try:
            last_day = (date(y, m + 1, 1) - timedelta(days=1)).day if m < 12 else 31
        except ValueError:
            last_day = 31
        band_start = date(y, m, 1)
        band_end = date(y, m, last_day)
        if band_start < start:
            band_start = start
        if band_end > end:
            band_end = end
        bands.append(
            {
                "start": band_start.isoformat(),
                "end": band_end.isoformat(),
                "month": m,
                "label": phenology_stage_estimate(
                    crops, m, peak_month=peak_month, fallback_crop=fallback_crop
                ),
            }
        )
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
    return bands


def _conf_cn(level: str) -> str:
    return CONF_CN.get(level, level)


def compute_confidence(
    *,
    official_s2: int,
    peak_exists: bool,
    drought_counts: dict[str, Any] | None,
    s1_count: int,
    flood_scene_count: int,
    harvest: dict[str, Any] | None,
) -> dict[str, Any]:
    """Program RULES for 高/中/低 — never an AI percentage."""
    counts = drought_counts or {}
    unreliable = int(counts.get("unreliable") or 0)
    official = int(official_s2 or 0)

    if official >= 15 and peak_exists:
        growth_level = "high"
        growth_reason = f"官方可用景{official}且存在NDVI峰值"
    elif official >= 8 and peak_exists:
        growth_level = "medium"
        growth_reason = f"官方可用景{official}，峰值可识别但覆盖一般"
    else:
        growth_level = "low"
        growth_reason = f"官方可用景{official}不足或缺少峰值"

    if official < 4:
        drought_level = "low"
        drought_reason = f"官方可用景{official}过少，干旱判定不稳定"
    elif official < 8 or unreliable >= official:
        drought_level = "medium"
        drought_reason = (
            f"不可靠景{unreliable}较多或官方可用景{official}偏少，干旱置信度取中"
        )
    elif official >= 15:
        drought_level = "high"
        drought_reason = f"官方可用景{official}充足，干旱等级仅统计官方景"
    else:
        drought_level = "medium"
        drought_reason = f"官方可用景{official}，干旱判定取中"

    flood_n = int(flood_scene_count or 0)
    s1_n = int(s1_count or 0)
    if s1_n >= 8 and flood_n == 0:
        flood_level = "high"
        flood_reason = f"S1景{s1_n}且未检出洪涝，判定一致"
    elif s1_n >= 8:
        flood_level = "high"
        flood_reason = f"S1景{s1_n}，洪涝检出{flood_n}景且序列可对照"
    elif s1_n >= 4:
        flood_level = "medium"
        flood_reason = f"S1景{s1_n}，洪涝序列覆盖一般"
    else:
        flood_level = "low"
        flood_reason = f"S1景{s1_n}不足，洪涝判定不稳定"

    h = harvest or {}
    h_conf = str(h.get("confidence") or "").lower()
    h_status = str(h.get("status") or "")
    if h_conf == "low" or h_status in (
        "",
        "uncertain",
        "no_growth",
        "no_data",
        "not_detected",
    ):
        harvest_level = "low"
        harvest_reason = "程序收获置信度为低，或未形成稳定检测"
    elif h_conf == "high":
        harvest_level = "high"
        harvest_reason = "程序收获置信度为高"
    elif h_conf == "medium":
        harvest_level = "medium"
        harvest_reason = "程序收获置信度为中"
    else:
        harvest_level = "low"
        harvest_reason = "程序未给出中/高收获置信度，按低处理"

    items = {
        "growth": {
            "key": "growth",
            "label": "长势",
            "level": growth_level,
            "level_cn": _conf_cn(growth_level),
            "reason": growth_reason,
        },
        "drought": {
            "key": "drought",
            "label": "干旱",
            "level": drought_level,
            "level_cn": _conf_cn(drought_level),
            "reason": drought_reason,
        },
        "flood": {
            "key": "flood",
            "label": "洪涝",
            "level": flood_level,
            "level_cn": _conf_cn(flood_level),
            "reason": flood_reason,
        },
        "harvest": {
            "key": "harvest",
            "label": "收获",
            "level": harvest_level,
            "level_cn": _conf_cn(harvest_level),
            "reason": harvest_reason,
        },
    }
    return {
        "growth": items["growth"],
        "drought": items["drought"],
        "flood": items["flood"],
        "harvest": items["harvest"],
        "items": [items["growth"], items["drought"], items["flood"], items["harvest"]],
    }


def _latest_official_ndvi(series: list[dict[str, Any]]) -> dict[str, Any] | None:
    official = [p for p in series if p.get("official") is not False]
    if official:
        return official[-1]
    return series[-1] if series else None


def compute_status_cards(
    *,
    ndvi: dict[str, Any],
    drought: dict[str, Any],
    flood: dict[str, Any],
    harvest: dict[str, Any],
    scenes: dict[str, Any],
    confidence: dict[str, Any],
) -> list[dict[str, Any]]:
    """Cover cards: status text from program numbers + RULES confidence."""
    peak = ndvi.get("peak") or {}
    series = list(ndvi.get("series") or [])
    latest = _latest_official_ndvi(series) or ndvi.get("latest") or {}
    peak_v = _num(peak.get("value"))
    latest_v = _num(latest.get("value"))
    if peak_v and latest_v is not None and peak_v > 0:
        ratio = latest_v / peak_v
        if ratio < 0.70:
            growth_value = "冠层绿度较峰值回落"
        elif latest_v >= 0.60:
            growth_value = "冠层绿度较高"
        elif latest_v >= 0.35:
            growth_value = "冠层绿度中等"
        else:
            growth_value = "冠层绿度偏低"
    elif latest_v is not None:
        growth_value = "冠层绿度可观测"
    else:
        growth_value = "长势数据不足"
    growth_detail = (
        f"峰值{_fmt_idx(peak.get('value'))} @ {peak.get('date') or '—'}；"
        f"最新{_fmt_idx(latest.get('value'))} @ {latest.get('date') or '—'}"
    )

    counts = drought.get("counts") or {}
    drought_n = int(drought.get("drought_scene_count") or 0)
    severe = int(counts.get("severe") or 0)
    if drought_n <= 0:
        drought_value = "官方干旱景未检出"
    elif severe >= 3:
        drought_value = "偏干提示增多"
    else:
        drought_value = "提示存在干旱景"
    drought_detail = format_drought_counts_inline(counts)

    flood_n = int(flood.get("flood_scene_count") or 0)
    s1_n = int(scenes.get("s1_count") or flood.get("scene_count") or 0)
    if flood.get("status") == "no_s1_data":
        flood_value = "无S1数据"
        flood_detail = "窗口内无 Sentinel-1，无法判定洪涝"
    elif flood_n <= 0:
        flood_value = "未检出洪涝"
        flood_detail = f"S1 {s1_n} 景，洪涝 0"
    else:
        flood_value = "提示存在洪涝景"
        flood_detail = f"S1 {s1_n} 景，洪涝 {flood_n}"

    h_status = str(harvest.get("status") or "")
    h_conf = str(harvest.get("confidence") or "low")
    if h_status == "detected":
        harvest_value = "疑似进入成熟后期或收获准备阶段"
        date_s = harvest.get("harvest_date") or "—"
        harvest_detail = f"程序日期 {date_s}，需田间确认"
    elif h_status in ("uncertain", "no_growth"):
        harvest_value = "未形成稳定收获判定"
        harvest_detail = f"程序状态不确定（置信度{_conf_cn(h_conf)}）"
    else:
        harvest_value = "未检出收获信号"
        harvest_detail = "窗口内无稳定收获检测"

    conf_g = confidence.get("growth") or {}
    conf_d = confidence.get("drought") or {}
    conf_f = confidence.get("flood") or {}
    conf_h = confidence.get("harvest") or {}
    return [
        {
            "key": "growth",
            "title": "当前长势",
            "value": growth_value,
            "detail": growth_detail,
            "confidence": conf_g.get("level_cn") or "低",
            "confidence_level": conf_g.get("level") or "low",
        },
        {
            "key": "drought",
            "title": "水分状态",
            "value": drought_value,
            "detail": drought_detail,
            "confidence": conf_d.get("level_cn") or "中",
            "confidence_level": conf_d.get("level") or "medium",
        },
        {
            "key": "flood",
            "title": "洪涝监测",
            "value": flood_value,
            "detail": flood_detail,
            "confidence": conf_f.get("level_cn") or "低",
            "confidence_level": conf_f.get("level") or "low",
        },
        {
            "key": "harvest",
            "title": "成熟·收获",
            "value": harvest_value,
            "detail": harvest_detail,
            "confidence": conf_h.get("level_cn") or "低",
            "confidence_level": conf_h.get("level") or "low",
        },
    ]


def format_drought_counts_inline(counts: dict[str, Any] | None) -> str:
    if not counts:
        return "—"
    order = (
        ("severe", "重度"),
        ("moderate", "中度"),
        ("mild", "轻度"),
        ("normal", "正常"),
        ("unreliable", "不可靠"),
        ("out_of_season", "季外"),
    )
    parts = []
    for key, label in order:
        n = int(counts.get(key) or 0)
        if n > 0:
            parts.append(f"{label}{n}")
    return " / ".join(parts) if parts else "—"


def format_flood_counts_inline(counts: dict[str, Any] | None) -> str:
    if not counts:
        return "—"
    order = (
        ("flood_severe", "洪涝(重)"),
        ("flood_moderate", "洪涝"),
        ("watch", "关注"),
        ("dry", "正常"),
        ("unknown", "未定"),
    )
    parts = []
    for key, label in order:
        n = int(counts.get(key) or 0)
        if n > 0:
            parts.append(f"{label}{n}")
    return " / ".join(parts) if parts else "—"


def _fmt_idx(v: Any, digits: int = 3) -> str:
    n = _num(v)
    if n is None:
        return "—"
    return f"{n:.{digits}f}"


def compute_yoy(
    ndvi: dict[str, Any],
    prior: dict[str, Any] | None,
    scenes: dict[str, Any],
) -> dict[str, Any]:
    """Program-only year-over-year numbers. AI may only describe peak-date shift."""
    peak = ndvi.get("peak") or {}
    prior = prior or {}
    prior_peak = prior.get("ndvi_peak") or {}
    this_date = _parse_date(peak.get("date"))
    prior_date = _parse_date(prior_peak.get("date"))
    shift: dict[str, Any] | None = None
    if this_date and prior_date:
        delta = this_date.timetuple().tm_yday - prior_date.timetuple().tm_yday
        if delta < 0:
            shift = {
                "days": abs(delta),
                "direction": "提前",
                "label": f"峰值日期提前{abs(delta)}天",
            }
        elif delta > 0:
            shift = {
                "days": delta,
                "direction": "推后",
                "label": f"峰值日期推后{delta}天",
            }
        else:
            shift = {"days": 0, "direction": "相同", "label": "峰值日期相同"}
    return {
        "this_peak_date": peak.get("date"),
        "this_peak_value": peak.get("value"),
        "this_ndvi_mean": ndvi.get("mean"),
        "this_point_count": ndvi.get("point_count"),
        "this_official_count": scenes.get("s2_official_count"),
        "prior_start": prior.get("start_date"),
        "prior_end": prior.get("end_date"),
        "prior_peak_date": prior_peak.get("date"),
        "prior_peak_value": prior_peak.get("value"),
        "prior_ndvi_mean": prior.get("ndvi_mean"),
        "prior_point_count": prior.get("point_count"),
        "prior_official_count": prior.get("official_count"),
        "peak_date_shift": shift,
        "available": bool(this_date and prior_date),
    }


def compute_evidence_cards(
    *,
    ndvi: dict[str, Any],
    drought: dict[str, Any],
    flood: dict[str, Any],
    harvest: dict[str, Any],
    scenes: dict[str, Any],
    yoy: dict[str, Any],
) -> list[dict[str, str]]:
    peak = ndvi.get("peak") or {}
    latest = ndvi.get("latest") or {}
    growth = (
        f"官方可用景{scenes.get('s2_official_count', '—')} / 总{scenes.get('s2_count', '—')}；"
        f"NDVI峰值{_fmt_idx(peak.get('value'), 4)} @ {peak.get('date') or '—'}；"
        f"最新{_fmt_idx(latest.get('value'), 4)} @ {latest.get('date') or '—'}；"
        f"均值{_fmt_idx(ndvi.get('mean'), 4)}"
    )
    moisture = (
        f"官方干旱景{drought.get('drought_scene_count', 0)}；"
        f"{format_drought_counts_inline(drought.get('counts'))}"
    )
    flood_txt = (
        f"S1 {scenes.get('s1_count', 0)} 景；"
        f"洪涝{flood.get('flood_scene_count', 0)}；"
        f"{format_flood_counts_inline(flood.get('counts'))}；"
        f"VV中位数{_fmt_idx(flood.get('vv_median'), 3)} dB"
    )
    shift = (yoy.get("peak_date_shift") or {}).get("label") or "无上年峰值对比"
    h_status = harvest.get("status")
    if h_status == "detected":
        pheno = (
            f"峰值日期{peak.get('date') or '—'}；{shift}；"
            f"收获信号{harvest.get('harvest_date') or '—'}（需田间确认）。"
            "物候阶段为估计，非实测播种。"
        )
    else:
        pheno = (
            f"峰值日期{peak.get('date') or '—'}；{shift}；"
            "未形成稳定收获判定。物候阶段为估计，非实测播种。"
        )
    return [
        {"title": "长势", "body": growth},
        {"title": "水分", "body": moisture},
        {"title": "洪涝", "body": flood_txt},
        {"title": "物候", "body": pheno},
    ]


def program_core_conclusion(
    *,
    status_cards: list[dict[str, Any]],
    yoy: dict[str, Any],
    harvest: dict[str, Any],
) -> str:
    """≤80字 cautious one-liner from program numbers only."""
    g = next((c for c in status_cards if c["key"] == "growth"), {})
    d = next((c for c in status_cards if c["key"] == "drought"), {})
    f = next((c for c in status_cards if c["key"] == "flood"), {})
    shift = (yoy.get("peak_date_shift") or {}).get("label")
    parts = [g.get("value") or "长势可观测", d.get("value") or "", f.get("value") or ""]
    if harvest.get("status") == "detected":
        parts.append("疑似进入成熟后期或收获准备阶段")
    if shift:
        parts.append(shift)
    text = "，".join(p for p in parts if p)
    if len(text) > 80:
        text = text[:79] + "…"
    return text


def program_conclusions(
    *,
    scenes: dict[str, Any],
    ndvi: dict[str, Any],
    drought: dict[str, Any],
    flood: dict[str, Any],
    harvest: dict[str, Any],
    yoy: dict[str, Any],
) -> list[str]:
    peak = ndvi.get("peak") or {}
    latest = ndvi.get("latest") or {}
    items = [
        (
            f"官方可用景{scenes.get('s2_official_count', '—')}，"
            f"NDVI峰值{_fmt_idx(peak.get('value'), 4)}（{peak.get('date') or '—'}），"
            f"最新{_fmt_idx(latest.get('value'), 4)}（{latest.get('date') or '—'}），"
            "反映冠层绿度变化，不能据此推断产量。"
        ),
        (
            f"干旱分级（官方参与判定）："
            f"{format_drought_counts_inline(drought.get('counts'))}。"
            "九月绿度下降与干旱等级共现时，提示成熟脱水与天气偏干可能同时存在，"
            "缺少土壤/气象资料时不能定量区分。"
        ),
        (
            f"洪涝：S1 {scenes.get('s1_count', 0)} 景，"
            f"{format_flood_counts_inline(flood.get('counts'))}。"
        ),
    ]
    if harvest.get("status") == "detected":
        items.append(
            f"收获信号{harvest.get('harvest_date') or '—'}，"
            f"程序置信度{_conf_cn(str(harvest.get('confidence') or 'low'))}，"
            "疑似进入成熟后期或收获准备阶段，需田间确认，不得作为立即收割依据。"
        )
    else:
        items.append("窗口内未形成稳定收获判定，收获安排需结合田间确认。")
    shift = (yoy.get("peak_date_shift") or {}).get("label")
    if shift:
        items[0] = items[0] + f" 与上年相比仅能说明{shift}，不能推断生育进程整体提前。"
    return items[:4]


FOOTER_DISCLAIMER = (
    "声明：本报告全部数值（NDVI/NDMI/EVI/MNDWI、VV/VH、日期、景数、等级、收获信号）"
    "由程序计算；AI 仅作解读，不得编造天气、播种、品种、土壤、产量、墒情或成熟事实。"
    "干旱、洪涝与收获等判断为提示/可能/疑似结论，需进一步田间确认。"
    "不能仅凭 NDVI 推断产量损失；低置信度收获信号不得作为立即收割依据。"
    "九月绿度下降与干旱等级共现时，提示成熟脱水与天气偏干可能同时存在，"
    "在缺少土壤与气象资料时不能定量区分。物候阶段为估计，不代表实测播种日期。"
)



# ── Spatial RGB / NDVI pixel helpers ──────────────────────────────────

GROWTH_GRADE_ORDER = ("较好", "正常", "偏弱")
GROWTH_GRADE_RULE_ZH = "较好≥0.55 / 正常0.35–0.55 / 偏弱<0.35"


def classify_growth_grade(ndvi: float) -> str:
    """Map pixel NDVI to 较好/正常/偏弱."""
    if ndvi >= 0.55:
        return "较好"
    if ndvi >= 0.35:
        return "正常"
    return "偏弱"


def compute_growth_grade_shares(values: list[float]) -> dict[str, Any]:
    """Program-only grade shares from pixel NDVI values."""
    used = [float(v) for v in values if v is not None]
    counts = {g: 0 for g in GROWTH_GRADE_ORDER}
    for v in used:
        counts[classify_growth_grade(v)] += 1
    n = len(used)
    pct = {
        g: (round(counts[g] * 100.0 / n, 1) if n else 0.0) for g in GROWTH_GRADE_ORDER
    }
    return {
        "n": n,
        "counts": counts,
        "pct": pct,
        "rule_zh": GROWTH_GRADE_RULE_ZH,
        "labels": list(GROWTH_GRADE_ORDER),
    }


def _scene_has_rgb(row: dict[str, Any]) -> bool:
    return bool(row.get("rgb_url") or row.get("rgb_oss_key") or row.get("large_rgb_url"))


def _pick_rgb_scene(
    rows: list[dict[str, Any]],
    *,
    prefer_date: str | None = None,
    require_clear: bool = True,
) -> dict[str, Any] | None:
    """Pick usable S2 scene with RGB: official/clear preferred, then closest to prefer_date."""
    candidates = [r for r in rows if _scene_has_rgb(r)]
    if not candidates:
        return None

    def score(r: dict[str, Any]) -> tuple:
        cloud = r.get("cloud_pct")
        cloud_pen = float(cloud) if cloud is not None else 99.0
        official = 1 if r.get("official") else 0
        clear = 1 if r.get("clear") else 0
        date_dist = 0
        if prefer_date:
            pd = _parse_date(prefer_date)
            rd = _parse_date(r.get("date"))
            if pd and rd:
                date_dist = abs((rd - pd).days)
            else:
                date_dist = 9999
        # Prefer official/clear, then closest to prefer_date, then lower cloud
        return (official, clear if require_clear else 1, -date_dist, -cloud_pen)

    # Prefer clear+official first; fall back without require_clear
    usable = [r for r in candidates if r.get("official") or r.get("clear")]
    pool = usable or candidates
    if require_clear:
        clear_pool = [r for r in pool if r.get("clear")]
        if clear_pool:
            pool = clear_pool
    return max(pool, key=score)


def load_scene_lonlat_pixels(
    session: "Session",
    land_id: str,
    scene_date: str,
    *,
    prefer_clear: bool = True,
) -> list[dict[str, Any]]:
    """Load lonlat_v1 pixels for one land_id + date (sparse points, not a grid)."""
    from sqlalchemy import text

    if not land_id or not scene_date:
        return []
    row = (
        session.execute(
            text(
                """
            SELECT pixel_data
            FROM agri.parcel_scene_products
            WHERE land_id = :land_id AND sensor = 'S2' AND date = CAST(:d AS date)
              AND pixel_data->>'format' = 'lonlat_v1'
            ORDER BY
              CASE WHEN coalesce(parcel_cloud_cover_pct, cloud_cover) <= 20 THEN 0 ELSE 1 END,
              date DESC
            LIMIT 1
            """
            ),
            {"land_id": land_id, "d": str(scene_date)[:10]},
        )
        .mappings()
        .first()
    )
    if not row:
        return []
    pixel_data = row.get("pixel_data")
    if not isinstance(pixel_data, dict):
        return []
    raw = pixel_data.get("pixels")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for pix in raw:
        if not isinstance(pix, dict):
            continue
        lon, lat = pix.get("lon"), pix.get("lat")
        ndvi = pix.get("NDVI")
        if lon is None or lat is None or ndvi is None:
            continue
        try:
            float(lon)
            float(lat)
            float(ndvi)
        except (TypeError, ValueError):
            continue
        out.append(pix)
    if prefer_clear:
        cleared = [p for p in out if int(p.get("clear") or 0) == 1]
        if cleared:
            return cleared
    return out


def build_spatial_block(
    session: "Session | None",
    *,
    land_id: str | None,
    s2_rows: list[dict[str, Any]],
    ndvi_peak: dict[str, Any] | None,
    ndvi_latest: dict[str, Any] | None,
) -> dict[str, Any]:
    """Populate spatial facts: RGB URLs + optional pixel grade shares / points."""
    latest_target = (ndvi_latest or {}).get("date")
    peak_target = (ndvi_peak or {}).get("date")
    latest_scene = _pick_rgb_scene(s2_rows, prefer_date=latest_target)
    peak_scene = _pick_rgb_scene(s2_rows, prefer_date=peak_target)
    # Prefer distinct peak when peak date differs; else reuse latest
    if (
        peak_scene
        and latest_scene
        and peak_scene.get("date") == latest_scene.get("date")
        and peak_target
        and latest_target
        and str(peak_target)[:10] != str(latest_target)[:10]
    ):
        # try harder for a scene closer to peak
        alt = _pick_rgb_scene(s2_rows, prefer_date=peak_target, require_clear=False)
        if alt and alt.get("date") != latest_scene.get("date"):
            peak_scene = alt

    spatial: dict[str, Any] = {
        "has_pixel_stats": False,
        "has_anomaly_cluster": False,
        "rgb_url": None,
        "latest_rgb_url": None,
        "latest_rgb_date": None,
        "latest_large_rgb_url": None,
        "latest_rgb_oss_key": None,
        "peak_rgb_url": None,
        "peak_rgb_date": None,
        "peak_large_rgb_url": None,
        "peak_rgb_oss_key": None,
        "large_rgb_url": None,
        "rgb_local_path": None,
        "latest_rgb_path": None,
        "peak_rgb_path": None,
        "ndvi_local_path": None,
        "ndvi_map_path": None,
        "grade_shares": None,
        "pixel_points": None,
        "pixel_date": None,
        "pixel_format": None,
        "pixel_n": 0,
        "note": None,
    }

    if latest_scene:
        spatial["latest_rgb_url"] = latest_scene.get("rgb_url") or latest_scene.get(
            "large_rgb_url"
        )
        spatial["latest_rgb_date"] = latest_scene.get("date")
        spatial["latest_large_rgb_url"] = latest_scene.get("large_rgb_url")
        spatial["latest_rgb_oss_key"] = latest_scene.get("rgb_oss_key")
        spatial["rgb_url"] = spatial["latest_rgb_url"]
        spatial["large_rgb_url"] = latest_scene.get("large_rgb_url")

    if peak_scene:
        spatial["peak_rgb_url"] = peak_scene.get("rgb_url") or peak_scene.get(
            "large_rgb_url"
        )
        spatial["peak_rgb_date"] = peak_scene.get("date")
        spatial["peak_large_rgb_url"] = peak_scene.get("large_rgb_url")
        spatial["peak_rgb_oss_key"] = peak_scene.get("rgb_oss_key")

    # Load pixels for latest clear scene (prefer latest NDVI date, else peak)
    pixel_date = None
    for cand in (latest_scene, peak_scene):
        if cand and cand.get("pixel_n", 0) > 0 and cand.get("clear"):
            pixel_date = cand.get("date")
            break
    if pixel_date is None:
        for cand in (latest_scene, peak_scene):
            if cand and cand.get("pixel_n", 0) > 0:
                pixel_date = cand.get("date")
                break

    pixels: list[dict[str, Any]] = []
    if session is not None and land_id and pixel_date:
        try:
            pixels = load_scene_lonlat_pixels(session, land_id, str(pixel_date))
        except Exception as exc:  # noqa: BLE001
            logger.warning("load_scene_lonlat_pixels failed: %s", exc)
            pixels = []

    if pixels:
        vals = []
        points = []
        for p in pixels:
            v = _num(p.get("NDVI"))
            if v is None:
                continue
            vals.append(v)
            points.append(
                {
                    "lon": float(p["lon"]),
                    "lat": float(p["lat"]),
                    "ndvi": round(v, 4),
                    "clear": int(p.get("clear") or 0),
                }
            )
        shares = compute_growth_grade_shares(vals) if vals else None
        spatial["has_pixel_stats"] = bool(vals)
        spatial["grade_shares"] = shares
        spatial["pixel_points"] = points
        spatial["pixel_date"] = str(pixel_date)[:10]
        spatial["pixel_format"] = "lonlat_v1"
        spatial["pixel_n"] = len(points)
        spatial["note"] = (
            f"像元为 lonlat_v1 稀疏点（n={len(points)}），非规则栅格；"
            f"等级占比按程序阈值：{GROWTH_GRADE_RULE_ZH}。"
        )
    elif spatial.get("rgb_url") or spatial.get("peak_rgb_url"):
        spatial["note"] = "已登记真彩预览；像元级空间分级暂不可用或加载失败。"
    else:
        spatial["note"] = "当前版本暂未生成地块内部空间分级统计"

    return spatial


def build_season_facts(
    session: "Session",
    field_id: uuid.UUID | str,
    *,
    start_date: str,
    end_date: str,
    crops: list[str] | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Compute compact JSON facts for PDF + LLM (no invented metrics)."""
    from app.models.tables import Field

    fid = uuid.UUID(str(field_id))
    field = session.get(Field, fid)
    if not field or getattr(field, "deleted_at", None) is not None:
        raise ValueError("Field not found")

    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if not start or not end:
        raise ValueError("start_date and end_date required (YYYY-MM-DD)")
    if end < start:
        raise ValueError("end_date must be >= start_date")

    land_id = parse_agri_land_id(getattr(field, "tags_json", None))
    window = {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "crops": list(crops or []),
        "label": label,
    }
    season_months = sorted(months_from_window(window)) or list(
        range(start.month, end.month + 1) if start.year == end.year else [start.month]
    )

    field_meta = {
        "field_id": str(fid),
        "field_name": getattr(field, "name", None) or "地块",
        "land_id": land_id,
        "crop_type": getattr(field, "crop_type", None),
        "area_ha": float(field.area_ha) if getattr(field, "area_ha", None) else None,
    }

    if land_id:
        s2 = load_agri_s2_rows(session, land_id, start, end)
        s1 = load_agri_s1_rows(session, land_id, start, end)
        if not s2 and not s1:
            raise ValueError(
                f"窗口内无 agri 遥感数据 (land_id={land_id}, {start}~{end})"
            )
        ndvi_ts = [
            {"date": r["date"], "value": round(r["ndvi_avg"], 4), "official": r["official"]}
            for r in s2
            if r.get("ndvi_avg") is not None
        ]
        ndmi_ts = [
            {"date": r["date"], "value": round(r["ndmi_avg"], 4), "official": r["official"]}
            for r in s2
            if r.get("ndmi_avg") is not None
        ]
        official_s2 = [r for r in s2 if r.get("official")]
        clear_s2 = [r for r in s2 if r.get("clear")]
        harvest_pts = [
            {
                "date": r["date"],
                "ndvi_avg": r["ndvi_avg"],
                "scene_id": r.get("scene_id"),
                "official": r.get("official"),
                "decloud_quality": r.get("decloud_quality"),
            }
            for r in s2
            if r.get("ndvi_avg") is not None
        ]
        harvest = detect_harvest(harvest_pts, window=window)
        drought = _drought_summary(s2, season_months)
        flood = _flood_summary(s1)
        prior = _prior_year_comparison(
            session,
            land_id=land_id,
            field_id=fid,
            start=start,
            end=end,
            is_agri=True,
        )
        data_source = "agri.parcel_scene_products"
    else:
        indices = load_classic_indices(session, fid, start, end)
        ndvi_ts, ndmi_ts = _indices_to_ndvi_ndmi(indices, start, end)
        if len(ndvi_ts) < 2:
            raise ValueError(
                "非 agri 地块窗口内 NDVI 数据不足，无法生成生育期长势报告"
            )
        s2 = []
        s1 = []
        official_s2 = []
        clear_s2 = []
        harvest_pts = [
            {
                "date": p["date"],
                "ndvi_avg": p["value"],
                "scene_id": f"classic-{p['date']}",
                "official": True,
                "decloud_quality": "good",
            }
            for p in ndvi_ts
        ]
        harvest = detect_harvest(harvest_pts, window=window)
        # Approximate optical obs for drought from classic series
        pseudo = [
            {
                "date": p["date"],
                "ndvi_avg": p["value"],
                "ndmi_avg": next(
                    (m["value"] for m in ndmi_ts if m["date"] == p["date"]), None
                ),
                "official": True,
                "scene_id": f"classic-{p['date']}",
                "decloud_quality": "good",
                "cloud_cover": 0.0,
                "parcel_cloud_cover_pct": 0.0,
                "clear": True,
            }
            for p in ndvi_ts
        ]
        drought = _drought_summary(pseudo, season_months)
        flood = {
            "status": "not_applicable",
            "scene_count": 0,
            "vv_median": None,
            "counts": {},
            "scenes": [],
            "note": "经典地块无 S1 洪涝判定",
        }
        prior = _prior_year_comparison(
            session,
            land_id=None,
            field_id=fid,
            start=start,
            end=end,
            is_agri=False,
        )
        data_source = "field_stats"

    peak = _peak(ndvi_ts)
    latest = ndvi_ts[-1] if ndvi_ts else None
    harvest_dict = {
        "status": harvest.status,
        "harvest_date": harvest.harvest_date,
        "confidence": harvest.confidence,
        "scene_id": harvest.scene_id,
        "evidence": {
            k: harvest.evidence.get(k)
            for k in ("reason", "clear_point_count", "thresholds")
            if harvest.evidence and k in harvest.evidence
        },
    }

    s2_appendix = _build_s2_appendix(s2 if s2 else [], drought)
    # Classic path: rebuild appendix from pseudo-like ndvi if no agri s2
    if not s2 and ndvi_ts:
        s2_appendix = []
        class_by_date: dict[str, str] = {}
        for sc in drought.get("scene_classes") or []:
            d = sc.get("date")
            if d and d not in class_by_date:
                class_by_date[str(d)] = str(sc.get("class") or "")
        ndmi_by = {p["date"]: p.get("value") for p in ndmi_ts}
        for p in ndvi_ts:
            d = p["date"]
            cls = class_by_date.get(d)
            s2_appendix.append(
                {
                    "date": d,
                    "cloud_pct": 0.0,
                    "quality": "classic",
                    "quality_cn": quality_cn("classic"),
                    "drought_class": cls,
                    "drought_class_cn": drought_class_cn(cls),
                    "ndvi": p.get("value"),
                    "ndmi": ndmi_by.get(d),
                    "evi": None,
                    "mndwi": None,
                }
            )
    s1_appendix = _build_s1_appendix(flood)
    peak_month = None
    if peak and peak.get("date"):
        pd = _parse_date(peak.get("date"))
        peak_month = pd.month if pd else None
    timeline = _build_timeline(
        start=start,
        end=end,
        s2_rows=s2 if s2 else [{"date": p["date"], "official": True} for p in ndvi_ts],
        s1_rows=s1,
        drought=drought,
        flood=flood,
        ndvi_ts=ndvi_ts,
        crops=window.get("crops"),
        fallback_crop=field_meta.get("crop_type"),
        peak_month=peak_month,
    )

    confidence = compute_confidence(
        official_s2=len(official_s2),
        peak_exists=bool(peak),
        drought_counts=drought.get("counts"),
        s1_count=len(s1),
        flood_scene_count=int(flood.get("flood_scene_count") or 0),
        harvest=harvest_dict,
    )
    status_cards = compute_status_cards(
        ndvi={
            "series": ndvi_ts,
            "mean": round(_series_mean(ndvi_ts), 4) if _series_mean(ndvi_ts) is not None else None,
            "peak": peak,
            "latest": latest,
            "point_count": len(ndvi_ts),
        },
        drought=drought,
        flood=flood,
        harvest=harvest_dict,
        scenes={
            "s2_count": len(s2) if s2 else len(ndvi_ts),
            "s1_count": len(s1),
            "s2_official_count": len(official_s2),
            "s2_clear_count": len(clear_s2),
        },
        confidence=confidence,
    )
    yoy = compute_yoy(
        {
            "peak": peak,
            "mean": round(_series_mean(ndvi_ts), 4) if _series_mean(ndvi_ts) is not None else None,
            "point_count": len(ndvi_ts),
        },
        prior,
        {
            "s2_official_count": len(official_s2),
        },
    )
    evidence_cards = compute_evidence_cards(
        ndvi={
            "peak": peak,
            "latest": latest,
            "mean": round(_series_mean(ndvi_ts), 4) if _series_mean(ndvi_ts) is not None else None,
        },
        drought=drought,
        flood=flood,
        harvest=harvest_dict,
        scenes={
            "s2_count": len(s2) if s2 else len(ndvi_ts),
            "s1_count": len(s1),
            "s2_official_count": len(official_s2),
        },
        yoy=yoy,
    )
    conclusions = program_conclusions(
        scenes={
            "s2_official_count": len(official_s2),
            "s1_count": len(s1),
        },
        ndvi={"peak": peak, "latest": latest},
        drought=drought,
        flood=flood,
        harvest=harvest_dict,
        yoy=yoy,
    )
    core_line = program_core_conclusion(
        status_cards=status_cards, yoy=yoy, harvest=harvest_dict
    )
    phenology = phenology_bands(
        start,
        end,
        window.get("crops"),
        peak_month=peak_month,
        fallback_crop=field_meta.get("crop_type"),
    )

    facts: dict[str, Any] = {
        "field": field_meta,
        "window": window,
        "season_months": season_months,
        "data_source": data_source,
        "scenes": {
            "s2_count": len(s2) if s2 else len(ndvi_ts),
            "s1_count": len(s1),
            "s2_official_count": len(official_s2),
            "s2_clear_count": len(clear_s2),
        },
        "ndvi": {
            "series": ndvi_ts,
            "mean": round(_series_mean(ndvi_ts), 4)
            if _series_mean(ndvi_ts) is not None
            else None,
            "peak": peak,
            "latest": latest,
            "point_count": len(ndvi_ts),
        },
        "ndmi": {
            "series": ndmi_ts,
            "mean": round(_series_mean(ndmi_ts), 4)
            if _series_mean(ndmi_ts) is not None
            else None,
            "point_count": len(ndmi_ts),
        },
        "drought": drought,
        "flood": flood,
        "harvest": harvest_dict,
        "prior_year": prior,
        "methodology": _methodology(),
        "timeline": timeline,
        "s2_appendix": s2_appendix,
        "s1_appendix": s1_appendix,
        "confidence": confidence,
        "status_cards": status_cards,
        "evidence_cards": evidence_cards,
        "yoy": yoy,
        "phenology_estimate": phenology,
        "program_core_conclusion": core_line,
        "program_conclusions": conclusions,
        "disclaimer": FOOTER_DISCLAIMER,
        "spatial": build_spatial_block(
            session if land_id else None,
            land_id=land_id,
            s2_rows=s2 if s2 else [],
            ndvi_peak=peak,
            ndvi_latest=latest,
        ),
    }
    return facts


def facts_for_llm(
    facts: dict[str, Any],
    *,
    max_series: int = 24,
    max_drought_days: int = 10,
    max_s2_appendix: int = 30,
    max_s1_appendix: int = 20,
) -> dict[str, Any]:
    """Compact facts for Bailian prompt (truncate long series / day lists)."""
    ndvi = dict(facts.get("ndvi") or {})
    ndmi = dict(facts.get("ndmi") or {})
    # Keep scalars; drop bulky nested raw rows if present.
    for key in ("mean", "peak", "latest", "min", "max"):
        if key in (facts.get("ndvi") or {}):
            ndvi[key] = (facts.get("ndvi") or {}).get(key)
    series = list(ndvi.get("series") or [])
    if len(series) > max_series:
        step = max(1, len(series) // max_series)
        series = series[::step][:max_series]
        ndvi["series"] = series
        ndvi["series_truncated"] = True
        ndvi["series_original_n"] = len(list((facts.get("ndvi") or {}).get("series") or []))
    else:
        ndvi["series"] = series
        ndvi["series_truncated"] = False
    ndmi_series = list(ndmi.get("series") or [])
    if len(ndmi_series) > max_series:
        step = max(1, len(ndmi_series) // max_series)
        ndmi["series"] = ndmi_series[::step][:max_series]
        ndmi["series_truncated"] = True
    else:
        ndmi["series"] = ndmi_series
        ndmi["series_truncated"] = False
    drought_in = dict(facts.get("drought") or {})
    drought = {
        "drought_scene_count": drought_in.get("drought_scene_count"),
        "counts": drought_in.get("counts") or {},
        "days": list(drought_in.get("days") or [])[:max_drought_days],
        "days_truncated": len(list(drought_in.get("days") or [])) > max_drought_days,
    }
    flood_in = dict(facts.get("flood") or {})
    flood_scenes = list(flood_in.get("scenes") or [])
    # Prefer key flood/watch days; else truncate
    key_flood = [
        s
        for s in flood_scenes
        if s.get("class") in ("flood_severe", "flood_moderate", "watch")
    ]
    flood_scenes_out = (key_flood or flood_scenes)[:max_s1_appendix]
    flood = {
        "status": flood_in.get("status"),
        "scene_count": flood_in.get("scene_count"),
        "vv_median": flood_in.get("vv_median"),
        "flood_scene_count": flood_in.get("flood_scene_count"),
        "counts": flood_in.get("counts") or {},
        "note": flood_in.get("note"),
        "scenes": flood_scenes_out,
        "scenes_truncated": len(flood_scenes) > len(flood_scenes_out),
    }
    harvest = facts.get("harvest")
    if isinstance(harvest, dict):
        harvest = {
            k: harvest.get(k)
            for k in ("status", "harvest_date", "confidence", "scene_id", "method")
            if k in harvest or k in ("status", "harvest_date", "confidence")
        }
    prior = facts.get("prior_year")
    if isinstance(prior, dict):
        # Drop nested series from prior-year block if any.
        prior = {
            k: prior.get(k)
            for k in (
                "year",
                "start_date",
                "end_date",
                "ndvi_mean",
                "ndvi_peak",
                "drought_scene_count",
                "point_count",
                "scenes",
            )
            if k in prior
        }
    s2_app = list(facts.get("s2_appendix") or [])
    # Prefer drought days in truncated appendix
    drought_dates = {d.get("date") for d in (drought_in.get("days") or [])}
    if len(s2_app) > max_s2_appendix:
        preferred = [r for r in s2_app if r.get("date") in drought_dates]
        rest = [r for r in s2_app if r.get("date") not in drought_dates]
        s2_app_out = (preferred + rest)[:max_s2_appendix]
        s2_trunc = True
    else:
        s2_app_out = s2_app
        s2_trunc = False
    s1_app = list(facts.get("s1_appendix") or [])
    s1_app_out = s1_app[:max_s1_appendix]
    harvest_out = harvest
    if isinstance(harvest_out, dict) and harvest_out.get("status") == "detected":
        harvest_out = dict(harvest_out)
        harvest_out["wording_hint"] = (
            "疑似进入成熟后期或收获准备阶段（需田间确认，不得写立即收割）"
        )
    return {
        "field": facts.get("field"),
        "window": facts.get("window"),
        "season_months": facts.get("season_months"),
        "data_source": facts.get("data_source"),
        "scenes": facts.get("scenes"),
        "ndvi": ndvi,
        "ndmi": ndmi,
        "drought": drought,
        "flood": flood,
        "harvest": harvest_out,
        "prior_year": prior,
        "methodology": facts.get("methodology"),
        "timeline": facts.get("timeline") or [],
        "s2_appendix": s2_app_out,
        "s2_appendix_truncated": s2_trunc,
        "s1_appendix": s1_app_out,
        "s1_appendix_truncated": len(s1_app) > max_s1_appendix,
        "confidence": facts.get("confidence"),
        "status_cards": facts.get("status_cards"),
        "evidence_cards": facts.get("evidence_cards"),
        "yoy": facts.get("yoy"),
        "phenology_estimate": facts.get("phenology_estimate"),
        "program_core_conclusion": facts.get("program_core_conclusion"),
        "program_conclusions": facts.get("program_conclusions"),
        "disclaimer": facts.get("disclaimer"),
        "ai_rules": {
            "core_conclusion_max_chars": 90,
            "synthesis_chars": "120-180",
            "yoy_only": "只能写峰值日期提前/推后，禁止写生育进程提前一个月",
            "harvest_wording": "疑似进入成熟后期或收获准备阶段，需田间确认",
            "september_drought": "绿度下降与干旱共现=成熟脱水+天气偏干可能同时存在，不能定量",
            "ban": [
                "生物量积累达标",
                "生物量达标",
                "立即收割",
                "生育进程提前一个月",
                "排水良好",
                "排水条件良好",
                "无渍涝隐患",
                "收获窗口开启",
                "干旱风险提示偏高",
            ],
        },
    }


# ── Agronomy helpers for next-season / farming-risk pages ─────────────

_REMOTE_OPS_RE = re.compile(
    r"无人机|多源卫星|补测频次|采样密度|采样点|"
    r"遥感估产|增加.{0,8}卫星|云量影响|云量.{0,12}补|"
    r"遥感监测频次|雷达补测|多源遥感|卫星或无人机|"
    r"提升遥感|遥感数据受云"
)


def _flood_status_cn(status: Any) -> str:
    return {
        "ok": "正常监测",
        "no_s1_data": "无S1数据",
        "not_applicable": "不适用",
    }.get(str(status or ""), str(status or "—") or "—")


def looks_like_remote_ops_advice(text: str | None) -> bool:
    """True when advice pushes remote-sensing ops instead of field agronomy."""
    if not text:
        return False
    return bool(_REMOTE_OPS_RE.search(str(text)))


def _midlate_drought_days(days: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for d in days or []:
        ds = str(d.get("date") or "")
        if len(ds) >= 7 and ds[5:7] in ("07", "08", "09"):
            out.append(d)
    return out


def program_next_season_actions(
    *,
    drought: dict[str, Any] | None,
    flood: dict[str, Any] | None,
    harvest: dict[str, Any] | None = None,
) -> str:
    """Practical agronomy advice grounded in THIS season's program facts.

    Never suggests drones / multi-satellite / cloud-cover ops.
    """
    drought = drought or {}
    flood = flood or {}
    counts = drought.get("counts") or {}
    severe = int(counts.get("severe") or 0)
    moderate = int(counts.get("moderate") or 0)
    mild = int(counts.get("mild") or 0)
    drought_n = int(drought.get("drought_scene_count") or 0)
    if drought_n <= 0:
        drought_n = severe + moderate + mild
    days = list(drought.get("days") or [])
    midlate = _midlate_drought_days(days)
    severe_days = [d for d in days if d.get("class") == "severe"]

    flood_counts = flood.get("counts") or {}
    watch = int(flood_counts.get("watch") or 0)
    flood_n = int(flood.get("flood_scene_count") or 0)
    if flood_n <= 0:
        flood_n = int(flood_counts.get("flood_moderate") or 0) + int(
            flood_counts.get("flood_severe") or 0
        )

    caveat = "（基于本季遥感格局提示，需结合当地气象与田间确认）"
    lines: list[str] = []

    if drought_n > 0 or midlate:
        stage_hint = "拔节–抽雄、灌浆"
        if severe >= 3 or len(severe_days) >= 3 or len(midlate) >= 3:
            lines.append(
                f"本季中后期多次出现干旱/偏干信号（程序干旱景约{drought_n}），"
                f"下一季宜在{stage_hint}等关键阶段提前安排墒情检查与灌溉准备{caveat}。"
            )
        else:
            lines.append(
                f"本季有干旱提示（程序干旱景约{drought_n}），"
                f"下一季建议在{stage_hint}等关键阶段抽查墒情并预留灌溉能力{caveat}。"
            )
        lines.append("提前检修灌溉设施，预留应对伏旱/秋旱的供水能力。")
    else:
        lines.append(
            f"本季干旱信号不突出，仍建议下一季保留基本灌溉应急能力，"
            f"并在关键生育阶段抽查墒情{caveat}。"
        )

    if flood_n > 0 or watch > 0:
        lines.append(
            f"本季雷达曾出现关注/积水相关信号（关注{watch}、洪涝景{flood_n}），"
            f"下一季注意疏通沟渠、降低渍涝风险{caveat}。"
        )
    else:
        lines.append("本季未检出明显洪涝，雨季仍建议维护排水沟，避免局部积水。")

    lines.append(
        "记录实测播种日期、品种与产量，便于校准物候估计与解读下一季长势曲线。"
    )
    if harvest and harvest.get("status") == "detected":
        lines.append(
            "本季出现收获相关遥感信号，下一季可结合田间成熟观察记录，"
            "对照籽粒含水与收获窗口（仍须田间确认）。"
        )
    # Keep to a few short bullets for the card.
    return "\n".join(lines[:4])


def program_farming_risk_tips(
    *,
    drought: dict[str, Any] | None,
    flood: dict[str, Any] | None,
    harvest: dict[str, Any] | None = None,
    ndvi: dict[str, Any] | None = None,
) -> dict[str, list[str]]:
    """Program-owned farming risk cues for the dedicated risk page."""
    drought = drought or {}
    flood = flood or {}
    harvest = harvest or {}
    ndvi = ndvi or {}
    counts = drought.get("counts") or {}
    severe = int(counts.get("severe") or 0)
    moderate = int(counts.get("moderate") or 0)
    drought_n = int(drought.get("drought_scene_count") or 0) or (
        severe + moderate + int(counts.get("mild") or 0)
    )
    days = list(drought.get("days") or [])
    midlate = _midlate_drought_days(days)
    flood_counts = flood.get("counts") or {}
    watch = int(flood_counts.get("watch") or 0)
    flood_n = int(flood.get("flood_scene_count") or 0) or (
        int(flood_counts.get("flood_moderate") or 0)
        + int(flood_counts.get("flood_severe") or 0)
    )
    peak = ndvi.get("peak") or {}
    latest = ndvi.get("latest") or {}

    drought_tips: list[str] = []
    if drought_n > 0:
        dates_preview = "、".join(
            str(d.get("date")) for d in midlate[:4] if d.get("date")
        ) or "—"
        drought_tips.append(
            f"程序记录干旱/偏干景约{drought_n}（中后期示例日：{dates_preview}）。"
            "若田间墒情偏低，宜在拔节–抽雄、灌浆等需水关键期安排灌溉或补墒检查。"
        )
        if severe > 0:
            drought_tips.append(
                f"重度干旱分级共{severe}景：提示天气偏干与成熟脱水可能叠加，"
                "不能单凭遥感定量灾损，需对照土壤与气象。"
            )
    else:
        drought_tips.append(
            "本季官方干旱景不明显；仍建议在关键生育阶段抽查墒情，避免突发干热风。"
        )
    drought_tips.append("以上为基于本季遥感格局的提示，须结合当地气象与田间确认。")

    rain_tips: list[str] = []
    if flood_n > 0 or watch > 0:
        rain_tips.append(
            f"S1 监测：关注{watch}景、洪涝相关{flood_n}景。"
            "雨后检查排水沟与低洼积水，避免渍涝伤根。"
        )
    else:
        rain_tips.append(
            f"S1 状态：{_flood_status_cn(flood.get('status'))}；"
            f"{format_flood_counts_inline(flood_counts) or '未见明显积水信号'}。"
            "雨季仍建议保持沟渠畅通。"
        )
    rain_tips.append("雷达信号受轨道与地表结构影响，积水判断需田间复核。")

    harvest_tips: list[str] = []
    if harvest.get("status") == "detected":
        conf = _conf_cn(str(harvest.get("confidence") or "low"))
        harvest_tips.append(
            f"收获信号日 {harvest.get('harvest_date') or '—'}（程序置信度{conf}）："
            "疑似进入成熟后期或收获准备阶段，需田间确认籽粒含水与植株状态，"
            "不得作为立即收割依据。"
        )
    else:
        harvest_tips.append("窗口内未形成稳定收获判定；成熟与收获安排须田间确认。")
    if peak.get("date") and latest.get("date"):
        harvest_tips.append(
            f"NDVI峰值 {peak.get('date')}（{_fmt_idx(peak.get('value'), 4)}）→ "
            f"最新 {latest.get('date')}（{_fmt_idx(latest.get('value'), 4)}），"
            "绿度回落可与成熟脱水一致，亦可能叠加天气偏干，不能直接推断产量。"
        )
    harvest_tips.append("禁止仅凭低置信度遥感信号安排抢收。")

    return {
        "drought_irrigation": drought_tips[:4],
        "rain_drainage": rain_tips[:4],
        "harvest_maturity": harvest_tips[:4],
    }


def program_key_dates(
    *,
    ndvi: dict[str, Any] | None,
    drought: dict[str, Any] | None,
    harvest: dict[str, Any] | None = None,
    max_items: int = 8,
) -> list[dict[str, str]]:
    """Compact key-date strip: peak NDVI, severe drought days, harvest signal."""
    ndvi = ndvi or {}
    drought = drought or {}
    harvest = harvest or {}
    items: list[dict[str, str]] = []
    peak = ndvi.get("peak") or {}
    if peak.get("date"):
        items.append(
            {
                "date": str(peak["date"]),
                "label": "NDVI峰值",
                "detail": f"{_fmt_idx(peak.get('value'), 4)}",
            }
        )
    severe_days = [
        d for d in (drought.get("days") or []) if d.get("class") == "severe"
    ]
    # Prefer mid-late; cap
    severe_days = sorted(severe_days, key=lambda x: str(x.get("date") or ""))
    if len(severe_days) > 3:
        # first, mid, last
        severe_pick = [severe_days[0], severe_days[len(severe_days) // 2], severe_days[-1]]
    else:
        severe_pick = severe_days
    for d in severe_pick:
        if d.get("date"):
            items.append(
                {
                    "date": str(d["date"]),
                    "label": "重度干旱日",
                    "detail": "程序干旱分级",
                }
            )
    if not severe_pick:
        # show a couple of any drought days if present
        for d in list(drought.get("days") or [])[:2]:
            if d.get("date"):
                items.append(
                    {
                        "date": str(d["date"]),
                        "label": f"干旱日（{drought_class_cn(d.get('class'))}）",
                        "detail": "程序干旱分级",
                    }
                )
    if harvest.get("status") == "detected" and harvest.get("harvest_date"):
        conf = _conf_cn(str(harvest.get("confidence") or "low"))
        items.append(
            {
                "date": str(harvest["harvest_date"]),
                "label": "收获信号日",
                "detail": f"置信度{conf}，需田间确认",
            }
        )
    # de-dupe by date+label, sort
    seen: set[tuple[str, str]] = set()
    uniq: list[dict[str, str]] = []
    for it in sorted(items, key=lambda x: x.get("date") or ""):
        key = (it.get("date") or "", it.get("label") or "")
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    return uniq[:max_items]
