# -*- coding: utf-8 -*-
"""ReportLab Chinese PDF for 生育期长势分析报告."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.reports.land_assessment.paths import FONT_PATH

CST = timezone(timedelta(hours=8))


def _register_fonts() -> None:
    if "CN" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("CN", str(FONT_PATH)))
        pdfmetrics.registerFont(TTFont("CNB", str(FONT_PATH)))


def _styles() -> dict[str, ParagraphStyle]:
    return {
        "cover_title": ParagraphStyle(
            "sg_cover_title",
            fontName="CNB",
            fontSize=20,
            leading=28,
            alignment=TA_CENTER,
            textColor=HexColor("#143d2b"),
        ),
        "cover_sub": ParagraphStyle(
            "sg_cover_sub",
            fontName="CN",
            fontSize=11,
            leading=16,
            alignment=TA_CENTER,
            textColor=HexColor("#5a6a60"),
        ),
        "h1": ParagraphStyle(
            "sg_h1",
            fontName="CNB",
            fontSize=13,
            leading=20,
            textColor=HexColor("#143d2b"),
            spaceBefore=10,
            spaceAfter=6,
        ),
        "body": ParagraphStyle(
            "sg_body",
            fontName="CN",
            fontSize=10,
            leading=16,
            alignment=TA_JUSTIFY,
            textColor=HexColor("#222"),
        ),
        "small": ParagraphStyle(
            "sg_small",
            fontName="CN",
            fontSize=8.5,
            leading=13,
            textColor=HexColor("#666"),
        ),
        "bullet": ParagraphStyle(
            "sg_bullet",
            fontName="CN",
            fontSize=9.5,
            leading=15,
            leftIndent=8,
            textColor=HexColor("#222"),
        ),
        "left": ParagraphStyle(
            "sg_left",
            fontName="CN",
            fontSize=10,
            leading=15,
            alignment=TA_LEFT,
            textColor=HexColor("#222"),
        ),
    }


def _esc(s: Any) -> str:
    t = "" if s is None else str(s)
    return (
        t.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _fmt(v: Any, digits: int = 3) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def _table(rows: list[list[str]], col_widths: list[float] | None = None) -> Table:
    data = [[Paragraph(_esc(c), _styles()["small"]) for c in row] for row in rows]
    t = Table(data, colWidths=col_widths)
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), HexColor("#e8f0ea")),
                ("FONTNAME", (0, 0), (-1, -1), "CN"),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("GRID", (0, 0), (-1, -1), 0.4, HexColor("#c5d5c8")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return t


def render_season_growth_pdf(
    *,
    facts: dict[str, Any],
    ai: dict[str, Any] | None,
    chart_path: Path | str | None,
    materials_meta: list[dict[str, Any]] | None,
    out_path: Path | str,
) -> Path:
    _register_fonts()
    styles = _styles()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    field = facts.get("field") or {}
    window = facts.get("window") or {}
    scenes = facts.get("scenes") or {}
    ndvi = facts.get("ndvi") or {}
    drought = facts.get("drought") or {}
    flood = facts.get("flood") or {}
    harvest = facts.get("harvest") or {}
    ai = ai or {}
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")

    doc = SimpleDocTemplate(
        str(out),
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title="生育期长势分析报告",
    )
    story: list[Any] = []

    # 1. Cover
    story.append(Spacer(1, 18 * mm))
    story.append(Paragraph("生育期长势分析报告", styles["cover_title"]))
    story.append(Spacer(1, 6 * mm))
    season_label = window.get("label") or f"{window.get('start_date')} ~ {window.get('end_date')}"
    crops = window.get("crops") or []
    crops_s = "、".join(str(c) for c in crops) if crops else "—"
    cover_lines = [
        f"地块：{_esc(field.get('field_name'))}",
        f"land_id：{_esc(field.get('land_id') or '—')}",
        f"生育期窗口：{_esc(season_label)}",
        f"日期：{_esc(window.get('start_date'))} ~ {_esc(window.get('end_date'))}",
        f"作物：{_esc(crops_s)}",
        f"报告生成：{_esc(now)}",
    ]
    for line in cover_lines:
        story.append(Paragraph(line, styles["cover_sub"]))
    story.append(Spacer(1, 8 * mm))

    # 2. 摘要
    story.append(Paragraph("一、摘要", styles["h1"]))
    one = ai.get("one_liner") or "（无 AI 一句话摘要）"
    story.append(Paragraph(_esc(one), styles["body"]))
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph(_esc(ai.get("summary") or "—"), styles["body"]))
    for b in ai.get("evidence_bullets") or []:
        story.append(Paragraph(f"• {_esc(b)}", styles["bullet"]))

    # 3. 遥感事实
    story.append(Paragraph("二、生育期遥感事实", styles["h1"]))
    fact_rows = [
        ["项目", "数值"],
        ["S2 景数", str(scenes.get("s2_count", "—"))],
        ["S2 官方/可用景数", str(scenes.get("s2_official_count", "—"))],
        ["S2 晴空景数", str(scenes.get("s2_clear_count", "—"))],
        ["S1 景数", str(scenes.get("s1_count", "—"))],
        ["NDVI 均值", _fmt(ndvi.get("mean"), 4)],
        ["NDVI 峰值", f"{_fmt((ndvi.get('peak') or {}).get('value'), 4)} @ {(ndvi.get('peak') or {}).get('date') or '—'}"],
        ["最新 NDVI", f"{_fmt((ndvi.get('latest') or {}).get('value'), 4)} @ {(ndvi.get('latest') or {}).get('date') or '—'}"],
        ["NDMI 均值", _fmt((facts.get("ndmi") or {}).get("mean"), 4)],
        ["干旱景数(轻/中/重合计)", str(drought.get("drought_scene_count", "—"))],
        ["干旱分级计数", _esc(drought.get("counts") or {})],
        ["洪涝状态", _esc(flood.get("status"))],
        ["洪涝景数", str(flood.get("flood_scene_count", flood.get("counts", {}).get("flood_severe", "—")))],
        ["S1 VV 中位数", _fmt(flood.get("vv_median"), 3)],
        ["收获检测", f"{harvest.get('status')} / {harvest.get('harvest_date') or '—'} ({harvest.get('confidence') or '—'})"],
    ]
    prior = facts.get("prior_year")
    if prior:
        fact_rows.append(
            [
                "上年同期 NDVI 均值",
                f"{_fmt(prior.get('ndvi_mean'), 4)}（{prior.get('start_date')}~{prior.get('end_date')}，n={prior.get('point_count')}）",
            ]
        )
    story.append(_table(fact_rows, col_widths=[55 * mm, 115 * mm]))
    if flood.get("note"):
        story.append(Paragraph(_esc(flood["note"]), styles["small"]))

    # 4. 曲线图
    story.append(Paragraph("三、长势曲线图", styles["h1"]))
    if chart_path and Path(chart_path).exists():
        img = Image(str(chart_path), width=160 * mm, height=80 * mm)
        story.append(img)
    else:
        story.append(Paragraph("窗口内无足够 NDVI/NDMI 点，未生成曲线图。", styles["body"]))

    # 5. 材料
    story.append(Paragraph("四、材料说明", styles["h1"]))
    mats = materials_meta or []
    if not mats:
        story.append(Paragraph("本次未上传附加材料。", styles["body"]))
    else:
        for m in mats:
            line = f"• {_esc(m.get('filename'))}"
            if m.get("note"):
                line += f"（{_esc(m.get('note'))}）"
            elif m.get("ok"):
                line += "（已提取/已登记）"
            story.append(Paragraph(line, styles["bullet"]))

    # 6. 分析与建议
    story.append(Paragraph("五、分析与建议", styles["h1"]))
    story.append(
        Paragraph(_esc(ai.get("interpretation") or "（无 AI 解读）"), styles["body"])
    )
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph("建议", styles["left"]))
    story.append(
        Paragraph(_esc(ai.get("recommendations") or "—"), styles["body"])
    )
    if ai.get("llm_configured") is False:
        story.append(
            Paragraph(
                "说明：未配置 BAILIAN_API_KEY，AI 章节为占位提示，数值均来自程序计算。",
                styles["small"],
            )
        )

    # 7. Footer / sources
    story.append(Spacer(1, 6 * mm))
    story.append(
        Paragraph(
            f"数据来源：{_esc(facts.get('data_source') or '遥感产品')}；"
            "程序计算事实；AI 仅作解读不编造数值。Sentinel-2 / Sentinel-1。",
            styles["small"],
        )
    )

    doc.build(story)
    return out
