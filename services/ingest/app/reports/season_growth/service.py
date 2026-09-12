# -*- coding: utf-8 -*-
"""Orchestrate season-growth PDF generation."""

from __future__ import annotations

import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.reports.season_growth.bailian import generate_season_narrative
from app.reports.season_growth.charts import render_ndvi_ndmi_chart
from app.reports.season_growth.facts import build_season_facts, facts_for_llm
from app.reports.season_growth.materials import download_material_keys
from app.reports.season_growth.pdf_render import render_season_growth_pdf


def season_growth_pdf_filename(
    field_name: str | None,
    label: str | None = None,
    when: datetime | None = None,
) -> str:
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
    season = (label or "生育期").strip() or "生育期"
    for ch in '/\\:*?"<>|\n\r\t':
        season = season.replace(ch, "_")
    return f"{name}--{season}--{dt.strftime('%Y-%m-%d')}-长势报告.pdf"


def generate_season_growth_pdf(
    *,
    session: Session,
    field_id: uuid.UUID | str,
    start_date: str,
    end_date: str,
    crops: list[str] | None = None,
    label: str | None = None,
    material_keys: list[str] | None = None,
    out_path: Path | str | None = None,
) -> dict[str, Any]:
    """Generate 生育期长势 PDF. Returns out_path + summary for job progress."""
    facts = build_season_facts(
        session,
        field_id,
        start_date=start_date,
        end_date=end_date,
        crops=crops,
        label=label,
    )
    material_text, materials_meta = download_material_keys(material_keys)
    llm_facts = facts_for_llm(facts)
    ai = generate_season_narrative(llm_facts, material_text)

    field_name = (facts.get("field") or {}).get("field_name")
    filename = season_growth_pdf_filename(field_name, label=label)

    with tempfile.TemporaryDirectory(prefix="season_growth_") as tmp:
        tmp_dir = Path(tmp)
        chart_path = render_ndvi_ndmi_chart(facts, tmp_dir / "ndvi_ndmi.png")
        dest = Path(out_path) if out_path else tmp_dir / filename
        if out_path:
            dest = Path(out_path)
        else:
            # Persist outside temp: caller may upload; write beside tmp then copy
            dest = Path(tempfile.gettempdir()) / f"season_growth_{uuid.uuid4().hex}.pdf"
        render_season_growth_pdf(
            facts=facts,
            ai=ai,
            chart_path=chart_path,
            materials_meta=materials_meta,
            out_path=dest,
        )
        # If we used internal temp chart only, PDF already embeds it; dest is final
        final_path = dest

    summary = {
        "one_liner": ai.get("one_liner"),
        "llm_configured": ai.get("llm_configured"),
        "llm_error": ai.get("error"),
        "scenes": facts.get("scenes"),
        "ndvi_mean": (facts.get("ndvi") or {}).get("mean"),
        "ndvi_peak": (facts.get("ndvi") or {}).get("peak"),
        "harvest": facts.get("harvest"),
        "drought_scene_count": (facts.get("drought") or {}).get("drought_scene_count"),
        "flood_status": (facts.get("flood") or {}).get("status"),
        "window": facts.get("window"),
        "materials": materials_meta,
        "field_name": field_name,
        "land_id": (facts.get("field") or {}).get("land_id"),
    }
    return {
        "out_path": str(final_path),
        "download_filename": filename,
        "facts": facts,
        "ai": ai,
        "summary": summary,
    }
