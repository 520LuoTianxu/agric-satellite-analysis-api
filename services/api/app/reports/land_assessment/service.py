# -*- coding: utf-8 -*-
"""Orchestrate land-assessment PDF generation."""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.reports.land_assessment.charts import (
    pick_phenology_stages,
    pick_phenology_year,
    render_charts,
)
from app.reports.land_assessment.data_loader import (
    load_agri_lonlat_pixels,
    load_agri_pixel_date_index,
    load_bundle_from_dir,
    load_field_bundle,
)
from app.reports.land_assessment.pdf_render import render_pdf
from app.reports.land_assessment.scoring import compute_assessment


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
    stages = pick_phenology_stages(
        by_date, year, pixel_dates=pixel_dates or None
    )
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


def generate_assessment_pdf(
    *,
    session: Session | None = None,
    field_id: uuid.UUID | str | None = None,
    data_dir: Path | str | None = None,
    out_path: Path | str | None = None,
) -> dict[str, Any]:
    """Generate 选地体检 PDF.

    Provide either ``session``+``field_id`` (DB) or ``data_dir`` (fixtures).
    Returns dict with out_path, scorecard summary, object payload.
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
        },
    )

    pixels_by_date = _load_stage_pixels(
        session if data_dir is None else None,
        field.get("land_id"),
        computed["by_date"],
    )

    tmp_root = Path(tempfile.mkdtemp(prefix="land_assess_"))
    charts_dir = tmp_root / "charts"
    chart_paths = render_charts(
        computed["by_date"],
        computed["meta"],
        charts_dir,
        pixels_by_date=pixels_by_date,
    )

    if out_path is None:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (field.get("name") or "field"))
        out_path = tmp_root / f"{safe}_选地分析报告.pdf"
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
    )

    ov = computed["scorecard"]["overall"]
    chart_names = sorted(k for k in chart_paths if isinstance(chart_paths[k], Path))
    return {
        "out_path": str(out_path),
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
        "charts_dir": str(charts_dir),
        "charts": chart_names,
    }
