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
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.reports.land_assessment.paths import FONT_PATH
from app.reports.season_growth.facts import drought_class_cn, flood_class_cn

CST = timezone(timedelta(hours=8))
_APPENDIX_MAX_ROWS = 80


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
        "h2": ParagraphStyle(
            "sg_h2",
            fontName="CNB",
            fontSize=11,
            leading=16,
            textColor=HexColor("#1b4332"),
            spaceBefore=8,
            spaceAfter=4,
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


def _resolve_chart_paths(
    chart_paths: dict[str, Path | str] | None,
    chart_path: Path | str | None,
) -> dict[str, Path]:
    out: dict[str, Path] = {}
    if chart_paths:
        for k, v in chart_paths.items():
            if v and Path(v).exists():
                out[str(k)] = Path(v)
    if chart_path and Path(chart_path).exists() and "ndvi_ndmi" not in out:
        out["ndvi_ndmi"] = Path(chart_path)
    return out


def _bullets(story: list[Any], items: list[Any] | None, styles: dict) -> None:
    for b in items or []:
        story.append(Paragraph(f"• {_esc(b)}", styles["bullet"]))


def _text_or_dash(story: list[Any], text: Any, styles: dict, empty: str = "—") -> None:
    story.append(Paragraph(_esc(text or empty), styles["body"]))


def render_season_growth_pdf(
    *,
    facts: dict[str, Any],
    ai: dict[str, Any] | None,
    chart_path: Path | str | None = None,
    chart_paths: dict[str, Path | str] | None = None,
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
    methodology = facts.get("methodology") or {}
    timeline = facts.get("timeline") or []
    s2_appendix = list(facts.get("s2_appendix") or [])
    s1_appendix = list(facts.get("s1_appendix") or [])
    ai = ai or {}
    charts = _resolve_chart_paths(chart_paths, chart_path)
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

    # ── 1. Cover ──
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
        f"Sentinel-2 景数：{_esc(scenes.get('s2_count', '—'))}　"
        f"Sentinel-1 景数：{_esc(scenes.get('s1_count', '—'))}",
        f"报告生成：{_esc(now)}",
    ]
    for line in cover_lines:
        story.append(Paragraph(line, styles["cover_sub"]))
    story.append(PageBreak())

    # ── 2. 摘要 ──
    story.append(Paragraph("一、摘要", styles["h1"]))
    one = ai.get("one_liner") or "（无 AI 一句话摘要）"
    story.append(Paragraph(_esc(one), styles["body"]))
    story.append(Spacer(1, 2 * mm))
    if ai.get("core_conclusion"):
        story.append(Paragraph("核心结论", styles["h2"]))
        _text_or_dash(story, ai.get("core_conclusion"), styles)
    story.append(Paragraph("摘要", styles["h2"]))
    _text_or_dash(story, ai.get("summary"), styles)
    if ai.get("evidence_bullets"):
        story.append(Paragraph("证据要点", styles["h2"]))
        _bullets(story, ai.get("evidence_bullets"), styles)
    story.append(PageBreak())

    # ── 3. 任务背景与资料 / 方法 ──
    story.append(Paragraph("二、任务背景与资料", styles["h1"]))
    story.append(Paragraph("判定方法说明", styles["h2"]))
    method_rows = [
        ["类别", "说明"],
        ["光学干旱（S2）", methodology.get("drought") or "基于 agri_classify 干旱分类器。"],
        ["SAR 洪涝（S1）", methodology.get("flood") or "基于 agri_classify 洪涝分类器。"],
        ["传感器", methodology.get("sensors") or "Sentinel-2 / Sentinel-1"],
    ]
    story.append(_table(method_rows, col_widths=[40 * mm, 130 * mm]))
    story.append(Spacer(1, 3 * mm))

    # ── 4. 材料说明 ──
    story.append(Paragraph("三、材料说明", styles["h1"]))
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
    story.append(PageBreak())

    # ── 5. 生育期水分证据 ──
    story.append(Paragraph("四、生育期水分证据", styles["h1"]))

    story.append(Paragraph("4.1 遥感事实摘要", styles["h2"]))
    fact_rows = [
        ["项目", "数值"],
        ["S2 景数", str(scenes.get("s2_count", "—"))],
        ["S2 官方/可用景数", str(scenes.get("s2_official_count", "—"))],
        ["S2 晴空景数", str(scenes.get("s2_clear_count", "—"))],
        ["S1 景数", str(scenes.get("s1_count", "—"))],
        ["NDVI 均值", _fmt(ndvi.get("mean"), 4)],
        [
            "NDVI 峰值",
            f"{_fmt((ndvi.get('peak') or {}).get('value'), 4)} @ "
            f"{(ndvi.get('peak') or {}).get('date') or '—'}",
        ],
        [
            "最新 NDVI",
            f"{_fmt((ndvi.get('latest') or {}).get('value'), 4)} @ "
            f"{(ndvi.get('latest') or {}).get('date') or '—'}",
        ],
        ["NDMI 均值", _fmt((facts.get("ndmi") or {}).get("mean"), 4)],
        ["干旱景数(轻/中/重合计)", str(drought.get("drought_scene_count", "—"))],
        ["干旱分级计数", _esc(drought.get("counts") or {})],
        ["洪涝状态", _esc(flood.get("status"))],
        [
            "洪涝景数",
            str(
                flood.get(
                    "flood_scene_count",
                    (flood.get("counts") or {}).get("flood_severe", "—"),
                )
            ),
        ],
        ["洪涝分级计数", _esc(flood.get("counts") or {})],
        ["S1 VV 中位数", _fmt(flood.get("vv_median"), 3)],
        [
            "收获检测",
            f"{harvest.get('status')} / {harvest.get('harvest_date') or '—'} "
            f"({harvest.get('confidence') or '—'})",
        ],
    ]
    prior = facts.get("prior_year")
    if prior:
        fact_rows.append(
            [
                "上年同期 NDVI 均值",
                f"{_fmt(prior.get('ndvi_mean'), 4)}（{prior.get('start_date')}~"
                f"{prior.get('end_date')}，n={prior.get('point_count')}）",
            ]
        )
    story.append(_table(fact_rows, col_widths=[55 * mm, 115 * mm]))
    if flood.get("note"):
        story.append(Paragraph(_esc(flood["note"]), styles["small"]))

    story.append(Paragraph("4.2 NDVI / NDMI 曲线（干旱分级着色）", styles["h2"]))
    ndvi_chart = charts.get("ndvi_ndmi")
    if ndvi_chart and ndvi_chart.exists():
        story.append(Image(str(ndvi_chart), width=160 * mm, height=80 * mm))
    else:
        story.append(
            Paragraph("窗口内无足够 NDVI/NDMI 点，未生成曲线图。", styles["body"])
        )

    story.append(Paragraph("4.3 Sentinel-1 VV 曲线", styles["h2"]))
    s1_chart = charts.get("s1_vv")
    if s1_chart and s1_chart.exists():
        story.append(Image(str(s1_chart), width=160 * mm, height=76 * mm))
    else:
        story.append(
            Paragraph("窗口内无 S1 VV 数据，未生成洪涝曲线图。", styles["body"])
        )

    story.append(Paragraph("4.4 水分证据 AI 解读", styles["h2"]))
    _text_or_dash(
        story,
        ai.get("moisture_analysis"),
        styles,
        empty="（无 AI 水分分析）",
    )
    story.append(PageBreak())

    # ── 6. 时间线 ──
    story.append(Paragraph("五、时间线", styles["h1"]))
    if timeline:
        tl_rows = [["月份", "S2景数", "S1景数", "干旱日", "洪涝", "关注"]]
        for row in timeline:
            tl_rows.append(
                [
                    str(row.get("month") or "—"),
                    str(row.get("s2_count", 0)),
                    str(row.get("s1_count", 0)),
                    str(row.get("drought_days", 0)),
                    str(row.get("flood_count", 0)),
                    str(row.get("watch_count", 0)),
                ]
            )
        story.append(
            _table(
                tl_rows,
                col_widths=[30 * mm, 25 * mm, 25 * mm, 25 * mm, 25 * mm, 25 * mm],
            )
        )
    else:
        story.append(Paragraph("无按月时间线事实。", styles["body"]))
    if ai.get("timeline_notes"):
        story.append(Paragraph("时间线说明", styles["h2"]))
        _text_or_dash(story, ai.get("timeline_notes"), styles)
    story.append(PageBreak())

    # ── 7. 综合分析与结论 ──
    story.append(Paragraph("六、综合分析与结论", styles["h1"]))
    _text_or_dash(story, ai.get("interpretation"), styles, empty="（无 AI 解读）")
    if ai.get("causes_ranked"):
        story.append(Paragraph("可能原因（排序）", styles["h2"]))
        _bullets(story, ai.get("causes_ranked"), styles)
    if ai.get("core_conclusion"):
        story.append(Paragraph("结论复述", styles["h2"]))
        _text_or_dash(story, ai.get("core_conclusion"), styles)
    story.append(PageBreak())

    # ── 8. 建议补充取证 ──
    story.append(Paragraph("七、建议补充取证", styles["h1"]))
    story.append(Paragraph("管理建议", styles["h2"]))
    _text_or_dash(story, ai.get("recommendations"), styles)
    story.append(Paragraph("建议补充取证", styles["h2"]))
    if ai.get("follow_up"):
        _bullets(story, ai.get("follow_up"), styles)
    else:
        story.append(Paragraph("（无补充取证建议）", styles["body"]))
    if ai.get("llm_configured") is False:
        story.append(
            Paragraph(
                "说明：未配置 BAILIAN_API_KEY，AI 章节为占位提示，数值均来自程序计算。",
                styles["small"],
            )
        )
    story.append(PageBreak())

    # ── 9. 附录 ──
    story.append(Paragraph("八、附录", styles["h1"]))

    story.append(Paragraph("附录 A：Sentinel-2 逐景表", styles["h2"]))
    if not s2_appendix:
        story.append(Paragraph("无 S2 逐景记录。", styles["body"]))
    else:
        truncated = len(s2_appendix) > _APPENDIX_MAX_ROWS
        rows_a = s2_appendix[:_APPENDIX_MAX_ROWS]
        s2_rows = [
            ["日期", "云量%", "质量", "干旱等级", "NDVI", "NDMI", "EVI", "MNDWI"]
        ]
        for r in rows_a:
            cls = r.get("drought_class_cn") or drought_class_cn(r.get("drought_class"))
            s2_rows.append(
                [
                    str(r.get("date") or "—"),
                    _fmt(r.get("cloud_pct"), 1),
                    str(r.get("quality") or "—"),
                    str(cls),
                    _fmt(r.get("ndvi"), 3),
                    _fmt(r.get("ndmi"), 3),
                    _fmt(r.get("evi"), 3),
                    _fmt(r.get("mndwi"), 3),
                ]
            )
        story.append(
            _table(
                s2_rows,
                col_widths=[
                    22 * mm,
                    16 * mm,
                    20 * mm,
                    22 * mm,
                    18 * mm,
                    18 * mm,
                    18 * mm,
                    20 * mm,
                ],
            )
        )
        if truncated:
            story.append(
                Paragraph(
                    f"注：共 {len(s2_appendix)} 行，附录仅展示前 {_APPENDIX_MAX_ROWS} 行。",
                    styles["small"],
                )
            )

    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph("附录 B：Sentinel-1 逐景表", styles["h2"]))
    if not s1_appendix:
        story.append(Paragraph("无 S1 逐景记录。", styles["body"]))
    else:
        truncated_b = len(s1_appendix) > _APPENDIX_MAX_ROWS
        rows_b = s1_appendix[:_APPENDIX_MAX_ROWS]
        s1_rows = [["日期", "轨道", "VV (dB)", "VH (dB)", "洪涝等级"]]
        for r in rows_b:
            cls = r.get("flood_class_cn") or flood_class_cn(r.get("flood_class"))
            s1_rows.append(
                [
                    str(r.get("date") or "—"),
                    str(r.get("relative_orbit") if r.get("relative_orbit") is not None else "—"),
                    _fmt(r.get("vv"), 2),
                    _fmt(r.get("vh"), 2),
                    str(cls),
                ]
            )
        story.append(
            _table(
                s1_rows,
                col_widths=[30 * mm, 25 * mm, 30 * mm, 30 * mm, 40 * mm],
            )
        )
        if truncated_b:
            story.append(
                Paragraph(
                    f"注：共 {len(s1_appendix)} 行，附录仅展示前 {_APPENDIX_MAX_ROWS} 行。",
                    styles["small"],
                )
            )

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
