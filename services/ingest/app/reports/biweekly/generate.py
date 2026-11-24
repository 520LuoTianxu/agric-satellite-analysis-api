"""Official download-machine entry for a biweekly planting PDF.

Celery generate_biweekly_report and any official regeneration must call this
function (or the Celery task that wraps it). It enriches facts, then renders
with official_pdf.render_official_biweekly_pdf.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.reports.biweekly.conclusion_context import attach_conclusion_context
from app.reports.biweekly.enrichment import enrich_official_report_facts
from app.reports.biweekly.official_pdf import render_official_biweekly_pdf
from app.reports.biweekly.photo_review import attach_photo_review


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
    figures = (
        Path(figures_dir)
        if figures_dir
        else dest_path.parent / f"{dest_path.stem}_figures"
    )
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
    if not offline:
        attach_photo_review(filled)
    attach_conclusion_context(filled)
    if "ai_narrative" not in filled:
        from app.reports.biweekly.narrative_ai import (
            empty_judgment,
            generate_metric_judgment,
        )

        try:
            filled["ai_narrative"] = generate_metric_judgment(filled)
        except Exception as exc:
            # 解读异常只记在报告数据上。数值 PDF 仍要生成并上传，
            # 不能因为模型失败把已经能出的报告记成生成失败，也不能改写成结论。
            from app.core.logging import logger

            logger.exception(
                "biweekly_narrative_failed",
                error_type=type(exc).__name__,
            )
            filled["ai_narrative"] = empty_judgment(
                configured=True,
                error=type(exc).__name__,
                model="",
            )
        narrative = filled.get("ai_narrative")
        if isinstance(narrative, dict):
            from app.reports.biweekly.narrative_ai import reader_note

            note = reader_note(narrative)
            if note:
                narrative["reader_note"] = note
            # 登记用的是调用方 facts。把诊断写回去，状态仍由任务按 PDF 是否上传决定。
            facts["ai_narrative"] = {
                key: narrative[key]
                for key in (
                    "llm_configured",
                    "model",
                    "error",
                    "reader_note",
                    "diagnostics",
                    "summary",
                    "changes",
                    "conclusion",
                    "field_notes",
                    "metric_notes",
                    "infield",
                    "surrounding",
                    "weather",
                )
                if key in narrative
            }
    # 渲染用的是 facts 的副本。页面读的是登记回 API 的这份 facts，
    # 所以 PDF 里已经写好的短句要写回来，不能只留在副本上。
    context = filled.get("conclusion_context")
    if isinstance(context, dict) and context.get("lines"):
        facts["conclusion_context"] = {"lines": list(context.get("lines") or [])}
    patterns = filled.get("field_patterns")
    if isinstance(patterns, dict):
        kept = {
            key: patterns.get(key)
            for key in ("persistent_lines", "uniformity_lines", "history_line")
            if patterns.get(key)
        }
        if kept:
            facts["field_patterns"] = kept
    water = filled.get("water_risk")
    if isinstance(water, dict) and water.get("lines"):
        facts["water_risk"] = {"lines": list(water.get("lines") or [])}
    rendered = render_official_biweekly_pdf(filled, dest_path, figures_dir=figures)
    if facts_sidecar:
        side = Path(facts_sidecar)
        side.parent.mkdir(parents=True, exist_ok=True)
        side.write_text(
            json.dumps(filled, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    return rendered
