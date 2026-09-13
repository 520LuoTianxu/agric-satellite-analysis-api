# -*- coding: utf-8 -*-
"""Deterministic season-growth facts from agri / field_stats."""

from __future__ import annotations

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
                   pixel_data->>'decloud_quality' AS decloud_quality
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
            }
        )
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
    return {
        "counts": dict(counts),
        "drought_scene_count": sum(
            counts.get(k, 0) for k in ("mild", "moderate", "severe")
        ),
        "days": days[:40],
        "scene_classes": scene_classes,
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
    return {
        "start_date": prior_start.isoformat(),
        "end_date": prior_end.isoformat(),
        "ndvi_mean": round(_series_mean(ndvi) or 0.0, 4)
        if _series_mean(ndvi) is not None
        else None,
        "ndvi_peak": _peak(ndvi),
        "point_count": len(ndvi),
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


def drought_class_cn(cls: str | None) -> str:
    if cls is None:
        return "缺测/未定"
    return DROUGHT_CLASS_CN.get(str(cls), str(cls))


def flood_class_cn(cls: str | None) -> str:
    if cls is None:
        return "缺测/未定"
    return FLOOD_CLASS_CN.get(str(cls), str(cls))


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
            "洪涝需同时满足 VV≤−17.0 dB、相对基线下降≥3 dB，且 VH 或 VV−VH 辅助条件；"
            "VV≤−15.0 dB 的近阈值情形标「关注」；仅 VV−VH 不会单独判洪涝。"
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
        out.append(
            {
                "date": d,
                "cloud_pct": r.get("cloud_pct"),
                "quality": r.get("decloud_quality") or ("official" if r.get("official") else "raw"),
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


def _build_timeline(
    *,
    start: date,
    end: date,
    s2_rows: list[dict[str, Any]],
    s1_rows: list[dict[str, Any]],
    drought: dict[str, Any],
    flood: dict[str, Any],
) -> list[dict[str, Any]]:
    """Month rows in window: program scene / drought / flood counts only."""
    months: list[tuple[int, int]] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append((y, m))
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1

    drought_days = {
        str(d.get("date")): str(d.get("class"))
        for d in (drought.get("days") or [])
    }
    flood_scenes = list(flood.get("scenes") or [])

    rows: list[dict[str, Any]] = []
    for yy, mm in months:
        prefix = f"{yy:04d}-{mm:02d}"
        s2_n = sum(1 for r in s2_rows if str(r.get("date", "")).startswith(prefix))
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
        rows.append(
            {
                "month": prefix,
                "s2_count": s2_n,
                "s1_count": s1_n,
                "drought_days": drought_n,
                "flood_count": flood_n,
                "watch_count": watch_n,
            }
        )
    return rows


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
                    "drought_class": cls,
                    "drought_class_cn": drought_class_cn(cls),
                    "ndvi": p.get("value"),
                    "ndmi": ndmi_by.get(d),
                    "evi": None,
                    "mndwi": None,
                }
            )
    s1_appendix = _build_s1_appendix(flood)
    timeline = _build_timeline(
        start=start,
        end=end,
        s2_rows=s2 if s2 else [{"date": p["date"]} for p in ndvi_ts],
        s1_rows=s1,
        drought=drought,
        flood=flood,
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
        "harvest": harvest,
        "prior_year": prior,
        "methodology": facts.get("methodology"),
        "timeline": facts.get("timeline") or [],
        "s2_appendix": s2_app_out,
        "s2_appendix_truncated": s2_trunc,
        "s1_appendix": s1_app_out,
        "s1_appendix_truncated": len(s1_app) > max_s1_appendix,
    }
