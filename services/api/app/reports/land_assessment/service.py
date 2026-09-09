# -*- coding: utf-8 -*-
"""Orchestrate land-assessment PDF generation."""

from __future__ import annotations

import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.reports.land_assessment.charts import (
    pick_phenology_stages,
    pick_phenology_year,
    render_charts,
)
from app.reports.land_assessment.data_loader import (
    build_flood_evidence,
    cache_media_images,
    load_agri_lonlat_pixels,
    load_agri_pixel_date_index,
    load_bundle_from_dir,
    load_field_bundle,
    load_oss_media_for_dates,
)
from app.reports.land_assessment.pdf_render import render_pdf
from app.reports.land_assessment.scoring import (
    build_narrative_bridge,
    compute_assessment,
    compute_phenology_stage_summary,
)



def assessment_pdf_filename(
    field_name: str | None,
    when: datetime | None = None,
) -> str:
    """Download/display name: `{地块名}地块--YYYY-MM-DD-分析报告.pdf`."""
    name = (field_name or "地块").strip() or "地块"
    for ch in '/\\:*?"<>|\n\r\t':
        name = name.replace(ch, "_")
    if not name.endswith("地块"):
        name = f"{name}地块"
    dt = when or datetime.now(ZoneInfo("Asia/Shanghai"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    else:
        dt = dt.astimezone(ZoneInfo("Asia/Shanghai"))
    return f"{name}--{dt.strftime('%Y-%m-%d')}-分析报告.pdf"


def _load_stage_pixels(
    session: Session | None,
    land_id: str | None,
    by_date: dict[str, dict[str, float]],
) -> dict[str, list[dict[str, Any]]]:
    """Pick phenology stage dates then load lonlat_v1 pixels for those dates."""
    if session is None or not land_id:
        return {}
    year = pick_phenology_year(by_date)
    if year is None:
        return {}

    index = load_agri_pixel_date_index(session, land_id, cloud_max=None)
    pixel_dates = {row["date"] for row in index if row.get("has_pixels")}
    stages = pick_phenology_stages(by_date, year, pixel_dates=pixel_dates or None)
    wanted = sorted({st["date"] for st in stages.values()})
    if not wanted:
        return {}

    pixels = load_agri_lonlat_pixels(
        session, land_id, wanted, prefer_clear=True, cloud_max=None
    )

    # If a stage date lacked pixels, try nearby index dates (±12d) with pixels
    if len(pixels) < len(wanted):
        from datetime import datetime

        extras: list[str] = []
        have = set(pixels)
        for d in wanted:
            if d in have:
                continue
            target = datetime.fromisoformat(d).date()
            near = sorted(
                (
                    abs((datetime.fromisoformat(r["date"]).date() - target).days),
                    r["date"],
                )
                for r in index
                if r.get("has_pixels") and r["date"] not in have
            )
            for delta, cand in near:
                if delta > 12:
                    break
                extras.append(cand)
                break
        if extras:
            more = load_agri_lonlat_pixels(
                session, land_id, extras, prefer_clear=True, cloud_max=None
            )
            pixels.update(more)
    return pixels


def _stage_media_maps(
    cached: dict[str, Path],
) -> tuple[dict[str, Path], dict[str, Path]]:
    """Split cache_media_images keys into date->rgb / date->heatmap for stages."""
    rgb: dict[str, Path] = {}
    hm: dict[str, Path] = {}
    for name, path in cached.items():
        if name.startswith("stage_rgb_"):
            rgb[name[len("stage_rgb_") :]] = path
        elif name.startswith("stage_hm_"):
            hm[name[len("stage_hm_") :]] = path
    return rgb, hm


def generate_assessment_pdf(
    *,
    session: Session | None = None,
    field_id: uuid.UUID | str | None = None,
    data_dir: Path | str | None = None,
    out_path: Path | str | None = None,
) -> dict[str, Any]:
    """Generate 选地体检 PDF.

    Provide either ``session``+``field_id`` (DB) or ``data_dir`` (fixtures).
    Returns dict with out_path, scorecard summary, flood_evidence, object payload.
    """
    if data_dir is not None:
        bundle = load_bundle_from_dir(Path(data_dir))
    else:
        if session is None or field_id is None:
            raise ValueError("session and field_id required when data_dir is omitted")
        fid = uuid.UUID(str(field_id))
        bundle = load_field_bundle(session, fid)

    field = bundle["field"]
    computed = compute_assessment(
        indices=bundle["indices"],
        soil=bundle["soil"],
        weather_summary=bundle["weather_summary"],
        weather_stress=bundle["weather_stress"],
        suitability=bundle["suitability"],
        field_meta={
            "boundary_source": field.get("boundary_source"),
            "land_id": field.get("land_id"),
            "crop_type": field.get("crop_type"),
        },
        weather_history=bundle.get("weather_history") or {},
    )

    land_id = field.get("land_id")
    fid_raw = field.get("id") or field_id
    open_water = (computed.get("rs") or {}).get("open_water_dates") or []

    flood_evidence = None
    if open_water and session is not None:
        flood_evidence = build_flood_evidence(
            session,
            field_id=fid_raw,
            land_id=land_id,
            open_water_dates=open_water,
        )
    elif open_water:
        # Offline fixtures: still expose dates + placeholder analysis
        flood_evidence = build_flood_evidence(
            None,
            field_id=None,
            land_id=None,
            open_water_dates=open_water,
        )

    pixels_by_date = _load_stage_pixels(
        session if data_dir is None else None,
        land_id,
        computed["by_date"],
    )

    # Stage OSS media (RGB / heatmap) for frontend-like 图三 panel
    stage_media: dict[str, dict[str, Any]] = {}
    if session is not None and land_id:
        year = pick_phenology_year(computed["by_date"])
        if year is not None:
            stages = pick_phenology_stages(
                computed["by_date"],
                year,
                pixel_dates=set(pixels_by_date.keys()) or None,
            )
            stage_dates = [st["date"] for st in stages.values()]
            if stage_dates:
                stage_media = load_oss_media_for_dates(session, land_id, stage_dates)

    tmp_root = Path(tempfile.mkdtemp(prefix="land_assess_"))
    charts_dir = tmp_root / "charts"
    media_dir = tmp_root / "media"
    cached = cache_media_images(
        flood_evidence, media_dir, also_stage_media=stage_media or None
    )
    stage_rgb, stage_hm = _stage_media_maps(cached)

    # Enrich analysis with phenology stage summary + narrative bridge
    analysis = computed.setdefault("analysis", {})
    year = pick_phenology_year(computed["by_date"])
    stages_for_summary: dict = {}
    if year is not None:
        stages_for_summary = pick_phenology_stages(
            computed["by_date"],
            year,
            pixel_dates=set(pixels_by_date.keys()) or None,
        )
    peak_months = set((computed.get("meta") or {}).get("peak_months") or [7, 8])
    pheno = compute_phenology_stage_summary(
        stages_for_summary,
        computed["by_date"],
        peak_months=peak_months,
    )
    analysis["phenology_stage_summary"] = pheno
    analysis["phenology_year"] = year
    analysis["narrative_bridge"] = build_narrative_bridge(
        soil_plain=analysis.get("soil_analysis_plain") or "",
        weather_plain=analysis.get("weather_history_plain") or "",
        grade_shares=analysis.get("ndvi_grade_shares"),
        phenology=pheno,
        crop_label=(computed.get("meta") or {}).get("crop_label") or "作物",
        peak_mean=float((computed.get("rs") or {}).get("peak_ndvi_mean") or 0),
        uncropped_years=(computed.get("rs") or {}).get("possible_uncropped_years"),
    )

    chart_paths = render_charts(
        computed["by_date"],
        computed["meta"],
        charts_dir,
        pixels_by_date=pixels_by_date,
        stage_rgb_paths=stage_rgb or None,
        stage_heatmap_paths=stage_hm or None,
        flood_evidence=flood_evidence,
        write_individual_stage_maps=False,
        analysis=analysis,
    )

    if out_path is None:
        out_path = tmp_root / assessment_pdf_filename(field.get("name"))
    out_path = Path(out_path)

    render_pdf(
        out_path=out_path,
        field=field,
        scorecard=computed["scorecard"],
        rs=computed["rs"],
        risk=computed["risk"],
        soil=bundle["soil"],
        weather_summary=bundle["weather_summary"],
        chart_paths=chart_paths,
        title_suffix="OpenFarm",
        flood_evidence=flood_evidence,
        analysis=analysis,
    )

    ov = computed["scorecard"]["overall"]
    chart_names = sorted(k for k in chart_paths if isinstance(chart_paths[k], Path))

    # Attach flood_evidence into scorecard method block for API consumers
    if flood_evidence is not None:
        mwd = computed["scorecard"].setdefault("method_wet_drought", {})
        mwd["flood_evidence"] = {
            "absolute_open_water_scenes": flood_evidence.get(
                "absolute_open_water_scenes"
            ),
            "selected_count": flood_evidence.get("selected_count"),
            "analysis": flood_evidence.get("analysis"),
            "all_dates": flood_evidence.get("all_dates"),
            "scenes": [
                {
                    "date": s.get("date"),
                    "wet_mean": s.get("wet_mean"),
                    "ndvi_mean": s.get("ndvi_mean"),
                    "kind": s.get("kind"),
                    "analysis": s.get("analysis"),
                    "precip_prior_15d": {
                        k: v
                        for k, v in (s.get("precip_prior_15d") or {}).items()
                        if k != "days"  # keep payload lighter; days stay in full object
                    },
                    "media": {
                        "preview_url": (s.get("media") or {}).get("preview_url"),
                        "rgb_url": (s.get("media") or {}).get("rgb_url"),
                        "heatmap_url": (s.get("media") or {}).get("heatmap_url"),
                        "has_oss": (s.get("media") or {}).get("has_oss"),
                    },
                }
                for s in (flood_evidence.get("scenes") or [])
            ],
        }
        # Full scenes with daily precip retained on top-level flood_evidence

    return {
        "out_path": str(out_path),
        "download_filename": assessment_pdf_filename(field.get("name")),
        "field_id": field.get("id"),
        "field_name": field.get("name"),
        "area_ha": field.get("area_ha"),
        "area_mu": round(float(field.get("area_ha") or 0) * 15, 1),
        "indices_source": bundle.get("indices_source"),
        "n_index_rows": len(bundle.get("indices") or []),
        "n_pixel_dates": len(pixels_by_date),
        "score": ov["score"],
        "grade": ov["grade"],
        "light": ov["light"],
        "one_liner": ov.get("one_liner"),
        "scorecard": computed["scorecard"],
        "rs": computed["rs"],
        "risk": computed["risk"],
        "flood_evidence": flood_evidence,
        "analysis": analysis,
        "charts_dir": str(charts_dir),
        "charts": chart_names,
    }
