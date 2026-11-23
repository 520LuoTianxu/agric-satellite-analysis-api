"""Official download-machine entry for a biweekly planting PDF.

Celery generate_biweekly_report and any official regeneration must call this
function (or the Celery task that wraps it). It enriches facts, then renders
with official_pdf.render_official_biweekly_pdf.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.reports.biweekly.enrichment import enrich_official_report_facts
from app.reports.biweekly.official_pdf import render_official_biweekly_pdf


def generate_official_biweekly_pdf(
    facts: dict[str, Any],
    dest: str | Path,
    *,
    figures_dir: str | Path | None = None,
    products: list[dict[str, Any]] | None = None,
    products_dir: str | Path | None = None,
    storage: Any | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    fetch_weather: Any | None = None,
    fetch_truecolor: Any | None = None,
    facts_sidecar: str | Path | None = None,
    offline: bool = False,
) -> Path:
    """Enrich official facts, then render the official PDF.

    ``offline=True`` 只使用传入的 facts 和像元产品，不访问对象存储、内部接口、气象和影像服务。
    """
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    figures = Path(figures_dir) if figures_dir else dest_path.parent / f"{dest_path.stem}_figures"
    filled = enrich_official_report_facts(
        facts,
        figures_dir=figures,
        products=products,
        products_dir=products_dir,
        storage=storage,
        latitude=latitude,
        longitude=longitude,
        fetch_weather=fetch_weather,
        fetch_truecolor=fetch_truecolor,
        offline=offline,
    )
    rendered = render_official_biweekly_pdf(filled, dest_path, figures_dir=figures)
    if facts_sidecar:
        side = Path(facts_sidecar)
        side.parent.mkdir(parents=True, exist_ok=True)
        side.write_text(json.dumps(filled, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return rendered
