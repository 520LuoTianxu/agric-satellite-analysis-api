# -*- coding: utf-8 -*-
"""ReportLab Chinese PDF for 生育期长势分析报告 (v2 layout)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.reports.land_assessment.paths import FONT_PATH
from app.reports.season_growth.facts import (
    FOOTER_DISCLAIMER,
    drought_class_cn,
    flood_class_cn,
    program_farming_risk_tips,
    program_key_dates,
    program_next_season_actions,
)

CST = timezone(timedelta(hours=8))
_APPENDIX_MAX_ROWS = 28
_CONTENT_W = 178 * mm

_QUALITY_CN = {
    "official": "官方",
    "good": "良好",
    "fair": "一般",
    "bad": "较差",
    "raw": "原始",
    "classic": "经典",
}

_QUALITY_RANK = {
    "official": 5,
    "good": 4,
    "fair": 3,
    "raw": 2,
    "classic": 2,
    "bad": 1,
}

_DROUGHT_COUNT_ORDER = (
    ("severe", "重度"),
    ("moderate", "中度"),
    ("mild", "轻度"),
    ("normal", "正常"),
    ("unreliable", "不可靠"),
    ("out_of_season", "季外"),
)

_FLOOD_COUNT_ORDER = (
    ("flood_severe", "洪涝(重)"),
    ("flood_moderate", "洪涝"),
    ("watch", "关注"),
    ("dry", "正常"),
    ("unknown", "未定"),
)

_FLOOD_STATUS_CN = {
    "ok": "正常监测",
    "no_s1_data": "无S1数据",
    "not_applicable": "不适用",
}

_HARVEST_STATUS_CN = {
    "detected": "已检测",
    "uncertain": "不确定",
    "not_detected": "未检测",
    "no_data": "无数据",
    "no_growth": "无明显旺长期",
}

_HARVEST_CONF_CN = {
    "high": "高",
    "medium": "中",
    "low": "低",
}

_CARD_ACCENT = {
    "growth": "#1b4332",
    "drought": "#c62828",
    "flood": "#1565c0",
    "harvest": "#ef6c00",
}

_CONF_BG = {
    "high": "#e8f5e9",
    "medium": "#fff8e1",
    "low": "#f5f5f5",
    "高": "#e8f5e9",
    "中": "#fff8e1",
    "低": "#f5f5f5",
}


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
            leading=26,
            alignment=TA_CENTER,
            textColor=HexColor("#143d2b"),
        ),
        "cover_subtitle": ParagraphStyle(
            "sg_cover_subtitle",
            fontName="CN",
            fontSize=11,
            leading=16,
            alignment=TA_CENTER,
            textColor=HexColor("#3d5a4a"),
        ),
        "cover_field": ParagraphStyle(
            "sg_cover_field",
            fontName="CNB",
            fontSize=14,
            leading=20,
            alignment=TA_CENTER,
            textColor=HexColor("#1b4332"),
        ),
        "h1": ParagraphStyle(
            "sg_h1",
            fontName="CNB",
            fontSize=13,
            leading=18,
            textColor=HexColor("#143d2b"),
            spaceBefore=6,
            spaceAfter=4,
        ),
        "h2": ParagraphStyle(
            "sg_h2",
            fontName="CNB",
            fontSize=10.5,
            leading=14,
            textColor=HexColor("#1b4332"),
            spaceBefore=5,
            spaceAfter=3,
        ),
        "body": ParagraphStyle(
            "sg_body",
            fontName="CN",
            fontSize=9.5,
            leading=14,
            alignment=TA_JUSTIFY,
            textColor=HexColor("#222"),
        ),
        "small": ParagraphStyle(
            "sg_small",
            fontName="CN",
            fontSize=8,
            leading=11,
            textColor=HexColor("#555"),
        ),
        "small_r": ParagraphStyle(
            "sg_small_r",
            fontName="CN",
            fontSize=8,
            leading=11,
            alignment=TA_RIGHT,
            textColor=HexColor("#222"),
        ),
        "small_c": ParagraphStyle(
            "sg_small_c",
            fontName="CN",
            fontSize=8,
            leading=11,
            alignment=TA_CENTER,
            textColor=HexColor("#222"),
        ),
        "th": ParagraphStyle(
            "sg_th",
            fontName="CNB",
            fontSize=8,
            leading=11,
            alignment=TA_LEFT,
            textColor=HexColor("#143d2b"),
        ),
        "caption": ParagraphStyle(
            "sg_caption",
            fontName="CN",
            fontSize=8,
            leading=11,
            alignment=TA_CENTER,
            textColor=HexColor("#555"),
            spaceBefore=1,
            spaceAfter=3,
        ),
        "bullet": ParagraphStyle(
            "sg_bullet",
            fontName="CN",
            fontSize=9,
            leading=13,
            leftIndent=8,
            textColor=HexColor("#222"),
        ),
        "left": ParagraphStyle(
            "sg_left",
            fontName="CN",
            fontSize=9.5,
            leading=14,
            alignment=TA_LEFT,
            textColor=HexColor("#222"),
        ),
        "meta_label": ParagraphStyle(
            "sg_meta_label",
            fontName="CN",
            fontSize=8.5,
            leading=12,
            textColor=HexColor("#555"),
        ),
        "meta_value": ParagraphStyle(
            "sg_meta_value",
            fontName="CN",
            fontSize=8.5,
            leading=12,
            textColor=HexColor("#222"),
        ),
        "card_title": ParagraphStyle(
            "sg_card_title",
            fontName="CN",
            fontSize=8,
            leading=11,
            alignment=TA_CENTER,
            textColor=white,
        ),
        "card_value": ParagraphStyle(
            "sg_card_value",
            fontName="CNB",
            fontSize=9,
            leading=12,
            alignment=TA_CENTER,
            textColor=HexColor("#143d2b"),
        ),
        "card_detail": ParagraphStyle(
            "sg_card_detail",
            fontName="CN",
            fontSize=7.2,
            leading=10,
            alignment=TA_CENTER,
            textColor=HexColor("#555"),
        ),
        "card_conf": ParagraphStyle(
            "sg_card_conf",
            fontName="CNB",
            fontSize=7.5,
            leading=10,
            alignment=TA_CENTER,
            textColor=HexColor("#1b4332"),
        ),
        "box": ParagraphStyle(
            "sg_box",
            fontName="CN",
            fontSize=9.5,
            leading=14,
            alignment=TA_JUSTIFY,
            textColor=HexColor("#1b4332"),
        ),
        "pill_num": ParagraphStyle(
            "sg_pill_num",
            fontName="CNB",
            fontSize=9,
            leading=12,
            alignment=TA_CENTER,
            textColor=white,
        ),
        "card_h": ParagraphStyle(
            "sg_card_h",
            fontName="CNB",
            fontSize=9,
            leading=12,
            alignment=TA_LEFT,
            textColor=HexColor("#143d2b"),
        ),
        "card_bullet": ParagraphStyle(
            "sg_card_bullet",
            fontName="CN",
            fontSize=8.2,
            leading=11.5,
            alignment=TA_LEFT,
            textColor=HexColor("#222"),
        ),
        "caution": ParagraphStyle(
            "sg_caution",
            fontName="CN",
            fontSize=8.5,
            leading=12,
            alignment=TA_LEFT,
            textColor=HexColor("#7a4a00"),
        ),
        "chip": ParagraphStyle(
            "sg_chip",
            fontName="CN",
            fontSize=7.8,
            leading=11,
            alignment=TA_LEFT,
            textColor=HexColor("#333"),
        ),
        "footer": ParagraphStyle(
            "sg_footer",
            fontName="CN",
            fontSize=7.5,
            leading=11,
            alignment=TA_JUSTIFY,
            textColor=HexColor("#666"),
        ),
    }


def _esc(s: Any) -> str:
    t = "" if s is None else str(s)
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt(v: Any, digits: int = 3) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def format_drought_counts(counts: dict[str, Any] | None) -> str:
    """Chinese drought counts, omit zeros: 重度8 / 中度1 / …"""
    if not counts:
        return "—"
    parts: list[str] = []
    for key, label in _DROUGHT_COUNT_ORDER:
        n = int(counts.get(key) or 0)
        if n > 0:
            parts.append(f"{label}{n}")
    known = {k for k, _ in _DROUGHT_COUNT_ORDER}
    for key, n in counts.items():
        if key in known:
            continue
        try:
            iv = int(n or 0)
        except (TypeError, ValueError):
            continue
        if iv > 0:
            parts.append(f"{drought_class_cn(str(key))}{iv}")
    return " / ".join(parts) if parts else "—"


def format_flood_counts(counts: dict[str, Any] | None) -> str:
    """Chinese flood counts, omit zeros."""
    if not counts:
        return "—"
    parts: list[str] = []
    for key, label in _FLOOD_COUNT_ORDER:
        n = int(counts.get(key) or 0)
        if n > 0:
            parts.append(f"{label}{n}")
    known = {k for k, _ in _FLOOD_COUNT_ORDER}
    for key, n in counts.items():
        if key in known:
            continue
        try:
            iv = int(n or 0)
        except (TypeError, ValueError):
            continue
        if iv > 0:
            parts.append(f"{flood_class_cn(str(key))}{iv}")
    return " / ".join(parts) if parts else "—"


def format_flood_status(status: Any) -> str:
    if status is None or status == "":
        return "—"
    s = str(status)
    return _FLOOD_STATUS_CN.get(s, s)


def format_harvest_line(harvest: dict[str, Any] | None) -> str:
    h = harvest or {}
    status = h.get("status")
    status_cn = _HARVEST_STATUS_CN.get(str(status), str(status) if status else "—")
    date_s = h.get("harvest_date") or "—"
    conf = h.get("confidence")
    conf_cn = _HARVEST_CONF_CN.get(str(conf), str(conf) if conf else "—")
    if status in (None, "", "not_detected", "no_data", "no_growth", "uncertain") and not h.get(
        "harvest_date"
    ):
        return f"{status_cn}（置信度{conf_cn}）" if conf else status_cn
    return f"{status_cn} / {date_s}（{conf_cn}）"


def quality_cn(quality: Any) -> str:
    if quality is None or quality == "":
        return "—"
    q = str(quality).strip().lower()
    return _QUALITY_CN.get(q, str(quality))


def _quality_rank(quality: Any) -> int:
    q = str(quality or "").strip().lower()
    return _QUALITY_RANK.get(q, 0)


def _ndvi_usable(ndvi: Any) -> bool:
    if ndvi is None:
        return False
    try:
        v = float(ndvi)
    except (TypeError, ValueError):
        return False
    return abs(v) > 1e-9


def filter_s2_appendix_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer usable S2 scenes: best quality per date; drop bad/raw NDVI~0 noise."""
    if not rows:
        return []
    by_date: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        d = str(r.get("date") or "")
        by_date.setdefault(d, []).append(r)

    out: list[dict[str, Any]] = []
    for d in sorted(by_date.keys()):
        group = by_date[d]
        usable = [r for r in group if _ndvi_usable(r.get("ndvi"))]
        candidates = usable if usable else group

        def sort_key(r: dict[str, Any]) -> tuple:
            q = str(r.get("quality") or "").lower()
            rank = _quality_rank(q)
            if q == "official" or r.get("official"):
                rank = max(rank, 5)
            ndvi_ok = 1 if _ndvi_usable(r.get("ndvi")) else 0
            not_bad = 0 if q == "bad" else 1
            return (ndvi_ok, not_bad, rank)

        best = max(candidates, key=sort_key)
        q = str(best.get("quality") or "").lower()
        if not _ndvi_usable(best.get("ndvi")) and q == "bad" and len(group) > 1:
            better = [
                r
                for r in group
                if _ndvi_usable(r.get("ndvi"))
                or str(r.get("quality") or "").lower() != "bad"
            ]
            if better:
                best = max(better, key=sort_key)
            else:
                continue
        if not _ndvi_usable(best.get("ndvi")) and q in ("bad", "raw") and not usable:
            continue
        out.append(best)
    return out


def filter_s1_appendix_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe S1 by date; keep the row with the lowest VV (more conservative)."""
    if not rows:
        return []
    by_date: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        d = str(r.get("date") or "")
        by_date.setdefault(d, []).append(r)
    out: list[dict[str, Any]] = []
    for d in sorted(by_date.keys()):
        group = by_date[d]

        def vv_key(r: dict[str, Any]) -> float:
            try:
                return float(r.get("vv"))
            except (TypeError, ValueError):
                return 999.0

        out.append(min(group, key=vv_key))
    return out


def _p(text: Any, style: ParagraphStyle) -> Paragraph:
    return Paragraph(_esc(text), style)


def _table(
    rows: list[list[Any]],
    col_widths: list[float] | None = None,
    *,
    numeric_cols: set[int] | None = None,
    zebra: bool = True,
    header_bg: str = "#dce8df",
) -> Table:
    """Tables with optional RIGHT-aligned numeric columns (appendix Round-2)."""
    styles = _styles()
    numeric_cols = set(numeric_cols or set())
    data: list[list[Any]] = []
    for i, row in enumerate(rows):
        cells = []
        for j, c in enumerate(row):
            if isinstance(c, Paragraph):
                cells.append(c)
            elif i == 0:
                # Dark brand headers need light title text
                if str(header_bg).lower() in ("#1b4332", "#1565c0"):
                    th_style = ParagraphStyle(
                        "sg_th_inv",
                        parent=styles["th"],
                        textColor=white,
                    )
                    cells.append(Paragraph(_esc(c), th_style))
                else:
                    cells.append(Paragraph(_esc(c), styles["th"]))
            elif j in numeric_cols:
                cells.append(Paragraph(_esc(c), styles["small_r"]))
            else:
                cells.append(Paragraph(_esc(c), styles["small"]))
        data.append(cells)
    t = Table(data, colWidths=col_widths, repeatRows=1)
    cmds: list[tuple] = [
        ("BACKGROUND", (0, 0), (-1, 0), HexColor(header_bg)),
        ("FONTNAME", (0, 0), (-1, -1), "CN"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.35, HexColor("#c5d5c8")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2.5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2.5),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]
    for j in numeric_cols:
        cmds.append(("ALIGN", (j, 1), (j, -1), "RIGHT"))
    if zebra:
        for i in range(1, len(rows)):
            if i % 2 == 0:
                cmds.append(("BACKGROUND", (0, i), (-1, i), HexColor("#f4f8f5")))
    t.setStyle(TableStyle(cmds))
    return t


def _meta_table(rows: list[tuple[str, str]]) -> Table:
    styles = _styles()
    data = []
    for label, value in rows:
        data.append(
            [
                Paragraph(_esc(label), styles["meta_label"]),
                Paragraph(_esc(value), styles["meta_value"]),
            ]
        )
    grid: list[list[Any]] = []
    i = 0
    while i < len(data):
        if i + 1 < len(data):
            grid.append(data[i] + data[i + 1])
            i += 2
        else:
            grid.append(data[i] + ["", ""])
            i += 1
    t = Table(grid, colWidths=[28 * mm, 61 * mm, 28 * mm, 61 * mm])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), HexColor("#f3f7f4")),
                ("BACKGROUND", (2, 0), (2, -1), HexColor("#f3f7f4")),
                ("FONTNAME", (0, 0), (-1, -1), "CN"),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("BOX", (0, 0), (-1, -1), 0.4, HexColor("#c5d5c8")),
                ("INNERGRID", (0, 0), (-1, -1), 0.3, HexColor("#d7e3da")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
            ]
        )
    )
    return t


def _status_cards_table(cards: list[dict[str, Any]]) -> Table:
    styles = _styles()
    if len(cards) < 4:
        # pad so cover always shows four slots
        keys = ["growth", "drought", "flood", "harvest"]
        titles = ["当前长势", "水分状态", "洪涝监测", "成熟·收获"]
        by_key = {c.get("key"): c for c in cards}
        cards = [
            by_key.get(
                k,
                {
                    "key": k,
                    "title": titles[i],
                    "value": "—",
                    "detail": "—",
                    "confidence": "低",
                    "confidence_level": "low",
                },
            )
            for i, k in enumerate(keys)
        ]
    col_w = 44.5 * mm
    inner_rows = []
    header = []
    values = []
    confs = []
    details = []
    for card in cards[:4]:
        header.append(Paragraph(_esc(card.get("title") or ""), styles["card_title"]))
        values.append(Paragraph(_esc(card.get("value") or "—"), styles["card_value"]))
        confs.append(
            Paragraph(f"置信度 {_esc(card.get('confidence') or '低')}", styles["card_conf"])
        )
        details.append(Paragraph(_esc(card.get("detail") or ""), styles["card_detail"]))
    inner_rows = [header, values, confs, details]
    t = Table(inner_rows, colWidths=[col_w] * 4)
    cmds: list[tuple] = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("BOX", (0, 0), (-1, -1), 0.4, HexColor("#c5d5c8")),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, HexColor("#d7e3da")),
        ("BACKGROUND", (0, 1), (-1, 3), HexColor("#fbfdfb")),
        ("TOPPADDING", (0, 1), (-1, 1), 5),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 5),
    ]
    for i, card in enumerate(cards[:4]):
        accent = _CARD_ACCENT.get(str(card.get("key") or ""), "#1b4332")
        cmds.append(("BACKGROUND", (i, 0), (i, 0), HexColor(accent)))
        bg = _CONF_BG.get(str(card.get("confidence_level") or card.get("confidence") or ""), "#f5f5f5")
        cmds.append(("BACKGROUND", (i, 2), (i, 2), HexColor(bg)))
    t.setStyle(TableStyle(cmds))
    return t


def _evidence_cards_table(cards: list[dict[str, Any]]) -> Table:
    styles = _styles()
    cells: list[Any] = []
    for card in cards[:4]:
        title = Paragraph(_esc(str(card.get("title") or "")), styles["h2"])
        body = Paragraph(_esc(card.get("body") or "—"), styles["small"])
        inner = Table([[title], [body]], colWidths=[86 * mm])
        inner.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), HexColor("#e8f0ea")),
                    ("BACKGROUND", (0, 1), (-1, 1), HexColor("#fbfdfb")),
                    ("BOX", (0, 0), (-1, -1), 0.4, HexColor("#c5d5c8")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        cells.append(inner)
    while len(cells) < 4:
        cells.append("")
    grid = [[cells[0], cells[1]], [cells[2], cells[3]]]
    t = Table(grid, colWidths=[89 * mm, 89 * mm])
    t.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 1.5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 1.5),
                ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
            ]
        )
    )
    return t


def _highlight_box(title: str, body: str, styles: dict) -> Table:
    data = [
        [Paragraph(_esc(title), styles["h2"])],
        [Paragraph(_esc(body or "—"), styles["box"])],
    ]
    t = Table(data, colWidths=[_CONTENT_W])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), HexColor("#dce8df")),
                ("BACKGROUND", (0, 1), (-1, 1), HexColor("#f4f8f5")),
                ("BOX", (0, 0), (-1, -1), 0.4, HexColor("#9db8a4")),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return t



def _items_from_value(value: Any, *, max_items: int = 6) -> list[str]:
    """Normalize str / list / None into short left-aligned bullet strings."""
    if value is None:
        return []
    raw: list[str] = []
    if isinstance(value, list):
        for x in value:
            s = str(x).strip()
            if s:
                raw.append(s)
    else:
        s = str(value).strip()
        if not s:
            return []
        # Prefer explicit newlines (LLM list joined by _as_str_or_none).
        if "\n" in s:
            raw = [p.strip() for p in s.splitlines() if p.strip()]
        else:
            # Split long Chinese sentences into short bullets when possible.
            parts = [p.strip() for p in s.replace("；", "。").split("。") if p.strip()]
            raw = parts if len(parts) > 1 else [s]
    cleaned: list[str] = []
    for item in raw:
        t = item.lstrip("•·-— ").strip()
        if t:
            cleaned.append(t)
    return cleaned[:max_items]


def _panel_card(
    title: str,
    bullets: list[str],
    *,
    header_bg: str,
    body_bg: str,
    border: str,
    width: float,
    styles: dict,
    title_color: str | None = None,
) -> Table:
    title_style = ParagraphStyle(
        f"sg_panel_title_{id(title)}_{int(width)}",
        parent=styles["card_h"],
        textColor=HexColor(title_color or "#143d2b"),
    )
    body_style = styles["card_bullet"]
    rows: list[list[Any]] = [[Paragraph(_esc(title), title_style)]]
    if bullets:
        for b in bullets:
            rows.append([Paragraph(f"• {_esc(b)}", body_style)])
    else:
        rows.append([Paragraph("—", body_style)])
    t = Table(rows, colWidths=[width])
    cmds: list[tuple] = [
        ("BACKGROUND", (0, 0), (-1, 0), HexColor(header_bg)),
        ("BACKGROUND", (0, 1), (-1, -1), HexColor(body_bg)),
        ("BOX", (0, 0), (-1, -1), 0.45, HexColor(border)),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (0, 0), 3),
        ("BOTTOMPADDING", (0, 0), (0, 0), 3),
        ("TOPPADDING", (0, 1), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 2),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
    ]
    t.setStyle(TableStyle(cmds))
    return t


def _conclusion_pills(items: list[str], styles: dict) -> KeepTogether:
    """3–4 short conclusion cards with numbered pills."""
    pills: list[Any] = []
    accent = HexColor("#1b4332")
    for i, item in enumerate(items[:4], start=1):
        num = Paragraph(str(i), styles["pill_num"])
        body = Paragraph(_esc(item), styles["card_bullet"])
        num_cell = Table([[num]], colWidths=[7 * mm])
        num_cell.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), accent),
                    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                    ("LEFTPADDING", (0, 0), (-1, -1), 1),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 1),
                ]
            )
        )
        row = Table([[num_cell, body]], colWidths=[9 * mm, _CONTENT_W - 9 * mm])
        row.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), HexColor("#f4f8f5")),
                    ("BOX", (0, 0), (-1, -1), 0.4, HexColor("#9db8a4")),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (0, 0), 3),
                    ("RIGHTPADDING", (0, 0), (0, 0), 2),
                    ("LEFTPADDING", (1, 0), (1, 0), 4),
                    ("RIGHTPADDING", (1, 0), (1, 0), 5),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        pills.append(row)
        pills.append(Spacer(1, 1.8 * mm))
    return KeepTogether(pills)


def _factor_cards_row(
    strong: list[str], mid: list[str], weak: list[str], styles: dict
) -> Table:
    gap = 2.5 * mm
    col_w = (_CONTENT_W - 2 * gap) / 3
    cards = [
        _panel_card(
            "已观察到",
            strong or ["程序未列出更强因果；以下仅作提示。"],
            header_bg="#c8e6c9",
            body_bg="#e8f5e9",
            border="#81c784",
            width=col_w,
            styles=styles,
            title_color="#1b5e20",
        ),
        _panel_card(
            "较可能",
            mid or ["—"],
            header_bg="#ffffff",
            body_bg="#ffffff",
            border="#1b4332",
            width=col_w,
            styles=styles,
            title_color="#e65100",
        ),
        _panel_card(
            "暂不能判断",
            weak
            or [
                "天气、播种、品种、土壤、产量、墒情均未由本系统观测，不能认定。"
            ],
            header_bg="#e0e0e0",
            body_bg="#f5f5f5",
            border="#9e9e9e",
            width=col_w,
            styles=styles,
            title_color="#424242",
        ),
    ]
    spaced = Table(
        [[cards[0], "", cards[1], "", cards[2]]],
        colWidths=[col_w, gap, col_w, gap, col_w],
    )
    spaced.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return spaced


def _action_cards_row(
    now_items: list[str],
    week_items: list[str],
    next_items: list[str],
    styles: dict,
) -> Table:
    gap = 2.5 * mm
    col_w = (_CONTENT_W - 2 * gap) / 3
    cards = [
        _panel_card(
            "田间核查清单",
            now_items
            or ["结合田间确认当前冠层与墒情，不宜仅凭遥感安排作业。"],
            header_bg="#ffffff",
            body_bg="#ffffff",
            border="#1b4332",
            width=col_w,
            styles=styles,
            title_color="#1b4332",
        ),
        _panel_card(
            "7日监测",
            week_items or ["未来7天关注墒情与植株脱水，结合气象安排农事。"],
            header_bg="#ffffff",
            body_bg="#ffffff",
            border="#1b4332",
            width=col_w,
            styles=styles,
            title_color="#1b4332",
        ),
        _panel_card(
            "下一季农艺",
            next_items
            or [
                "基于本季遥感干旱/水分格局，下一季宜准备灌溉与排水能力，"
                "并在拔节–抽雄、灌浆等关键阶段检查墒情；记录播种日期、品种与产量（需当地确认）。"
            ],
            header_bg="#ffffff",
            body_bg="#ffffff",
            border="#1b4332",
            width=col_w,
            styles=styles,
            title_color="#1b4332",
        ),
    ]
    spaced = Table(
        [[cards[0], "", cards[1], "", cards[2]]],
        colWidths=[col_w, gap, col_w, gap, col_w],
    )
    spaced.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return spaced


def _caution_banner(text: str, styles: dict) -> Table:
    data = [[Paragraph(f"⚠ {_esc(text)}", styles["caution"])]]
    t = Table(data, colWidths=[_CONTENT_W])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), HexColor("#fff3e0")),
                ("BOX", (0, 0), (-1, -1), 0.6, HexColor("#ef6c00")),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    return t


def _gap_chips(items: list[str], styles: dict) -> Table:
    """Compact 2-column bullet list for evidence gaps."""
    if not items:
        items = ["—"]
    cells: list[Any] = []
    for it in items:
        chip = Table(
            [[Paragraph(f"• {_esc(it)}", styles["chip"])]],
            colWidths=[86 * mm],
        )
        chip.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), HexColor("#eceff1")),
                    ("BOX", (0, 0), (-1, -1), 0.3, HexColor("#b0bec5")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 2.5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        cells.append(chip)
    if len(cells) % 2 == 1:
        cells.append("")
    grid: list[list[Any]] = []
    for i in range(0, len(cells), 2):
        grid.append([cells[i], cells[i + 1]])
    t = Table(grid, colWidths=[89 * mm, 89 * mm])
    t.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 1),
                ("RIGHTPADDING", (0, 0), (-1, -1), 1),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
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


def _chart_block(
    story: list[Any],
    *,
    path: Path | None,
    width_mm: float,
    height_mm: float,
    caption: str,
    missing: str,
    styles: dict,
) -> None:
    if path and path.exists():
        block = [
            Image(str(path), width=width_mm * mm, height=height_mm * mm),
            Paragraph(_esc(caption), styles["caption"]),
        ]
        story.append(KeepTogether(block))
    else:
        story.append(Paragraph(_esc(missing), styles["body"]))


def _fmt_area(area_ha: Any) -> str:
    if area_ha is None or area_ha == "":
        return "—"
    try:
        return f"{float(area_ha):.2f} ha"
    except (TypeError, ValueError):
        return str(area_ha)


def _crops_label(window: dict[str, Any], field: dict[str, Any]) -> str:
    crops = window.get("crops") or []
    if crops:
        return "、".join(str(c) for c in crops)
    return str(field.get("crop_type") or "—")


def _page_footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFillColor(HexColor("#6b7a70"))
    canvas.setFont("CN", 7.5)
    canvas.drawString(
        16 * mm,
        8 * mm,
        "程序计算事实 · AI 仅解读 · 需田间确认",
    )
    canvas.drawRightString(A4[0] - 16 * mm, 8 * mm, f"{doc.page}")
    canvas.restoreState()



def _confidence_bars(items: list[dict[str, Any]], styles: dict) -> Table:
    """Four horizontal confidence bars with one-line program reasons."""
    rows: list[list[Any]] = []
    level_w = {"high": 0.92, "medium": 0.58, "low": 0.28, "高": 0.92, "中": 0.58, "低": 0.28}
    level_color = {
        "high": "#2e7d32",
        "medium": "#ef6c00",
        "low": "#9e9e9e",
        "高": "#2e7d32",
        "中": "#ef6c00",
        "低": "#9e9e9e",
    }
    bar_w = 70 * mm
    for it in items[:4]:
        label = str(it.get("label") or it.get("key") or "—")
        level = str(it.get("level_cn") or it.get("level") or "低")
        reason = str(it.get("reason") or "—")
        frac = level_w.get(str(it.get("level") or level), 0.28)
        color = level_color.get(str(it.get("level") or level), "#9e9e9e")
        filled = max(8 * mm, bar_w * frac)
        empty = bar_w - filled
        bar = Table([[""]], colWidths=[filled])
        bar.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), HexColor(color)),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        track = Table([[bar, ""]], colWidths=[filled, empty])
        track.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), HexColor("#eceff1")),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        rows.append(
            [
                Paragraph(_esc(label), styles["small"]),
                Paragraph(_esc(level), styles["small"]),
                track,
                Paragraph(_esc(reason), styles["small"]),
            ]
        )
    t = Table(rows, colWidths=[22 * mm, 14 * mm, bar_w + 2 * mm, 70 * mm])
    t.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("BOX", (0, 0), (-1, -1), 0.35, HexColor("#c5d5c8")),
                ("BACKGROUND", (0, 0), (-1, -1), HexColor("#fbfdfb")),
            ]
        )
    )
    return t


def _placeholder_panel(text: str, styles: dict, *, height_mm: float = 42) -> Table:
    body = Paragraph(_esc(text), styles["caption"])
    t = Table([[body]], colWidths=[_CONTENT_W / 2 - 2 * mm], rowHeights=[height_mm * mm])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), HexColor("#f5f7f6")),
                ("BOX", (0, 0), (-1, -1), 0.45, HexColor("#b0bec5")),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return t


def _try_image(path_or_url: Any, *, max_w: float, max_h: float) -> Any | None:
    if not path_or_url:
        return None
    p = Path(str(path_or_url))
    if not p.exists():
        return None
    try:
        img = Image(str(p))
        img._restrictSize(max_w, max_h)
        return img
    except Exception:
        return None


def _dual_visual_row(
    facts: dict[str, Any],
    styles: dict,
    charts: dict[str, Path] | None = None,
) -> Table:
    spatial = facts.get("spatial") or {}
    charts = charts or {}
    rgb = _try_image(
        spatial.get("rgb_local_path")
        or spatial.get("latest_rgb_path")
        or charts.get("latest_rgb"),
        max_w=_CONTENT_W / 2 - 4 * mm,
        max_h=48 * mm,
    )
    ndvi = _try_image(
        spatial.get("ndvi_local_path")
        or spatial.get("ndvi_map_path")
        or spatial.get("latest_ndvi_path")
        or charts.get("ndvi_spatial"),
        max_w=_CONTENT_W / 2 - 4 * mm,
        max_h=48 * mm,
    )
    left = rgb or _placeholder_panel("空间图预留，当前版本暂无栅格结果", styles, height_mm=48)
    right = ndvi or _placeholder_panel("空间图预留，当前版本暂无栅格结果", styles, height_mm=48)
    rgb_date = spatial.get("latest_rgb_date") or "—"
    ndvi_date = spatial.get("pixel_date") or spatial.get("latest_rgb_date") or "—"
    grid = Table(
        [
            [left, right],
            [
                Paragraph(f"最新真彩（{rgb_date}）", styles["caption"]),
                Paragraph(f"NDVI 空间分布（{ndvi_date}）", styles["caption"]),
            ],
        ],
        colWidths=[_CONTENT_W / 2, _CONTENT_W / 2],
    )
    grid.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    return grid


def _yoy_visual(yoy: dict[str, Any], styles: dict) -> Table:
    this_p = yoy.get("this_peak") or {}
    prior_p = yoy.get("prior_peak") or {}
    # fall back to nested shapes used by compute_yoy
    if not this_p and yoy.get("this_ndvi_peak"):
        this_p = yoy.get("this_ndvi_peak") or {}
    shift = yoy.get("peak_date_shift") or {}
    left = [
        Paragraph("2026 峰值", styles["card_h"]),
        Paragraph(
            f"{_esc(this_p.get('value') if isinstance(this_p, dict) else '—')} @ "
            f"{_esc((this_p or {}).get('date') if isinstance(this_p, dict) else '—')}",
            styles["body"],
        ),
    ]
    # compute_yoy stores differently — handle both
    this_date = yoy.get("this_peak_date") or (this_p.get("date") if isinstance(this_p, dict) else None)
    this_val = yoy.get("this_peak_value") or (this_p.get("value") if isinstance(this_p, dict) else None)
    prior_date = yoy.get("prior_peak_date") or (prior_p.get("date") if isinstance(prior_p, dict) else None)
    prior_val = yoy.get("prior_peak_value") or (prior_p.get("value") if isinstance(prior_p, dict) else None)
    # Also from nested report facts
    if this_date is None and isinstance(yoy.get("this"), dict):
        peak = (yoy.get("this") or {}).get("ndvi_peak") or {}
        this_date, this_val = peak.get("date"), peak.get("value")
    card_l = _panel_card(
        "2026 峰值",
        [f"{this_val if this_val is not None else '—'} @ {this_date or '—'}"],
        header_bg="#ffffff",
        body_bg="#ffffff",
        border="#1b4332",
        width=_CONTENT_W / 2 - 3 * mm,
        styles=styles,
        title_color="#1b4332",
    )
    card_r = _panel_card(
        "2025 峰值",
        [f"{prior_val if prior_val is not None else '—'} @ {prior_date or '—'}"],
        header_bg="#ffffff",
        body_bg="#ffffff",
        border="#1565c0",
        width=_CONTENT_W / 2 - 3 * mm,
        styles=styles,
        title_color="#1565c0",
    )
    row = Table([[card_l, card_r]], colWidths=[_CONTENT_W / 2, _CONTENT_W / 2])
    row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    note = Paragraph(
        f"程序计算：{_esc(shift.get('label') or '峰值日期对比不可用')}。"
        "峰值日期提前/推后 ≠ 物候进程提前相应天数（例如提前35天），不得据此推断播种或积温。",
        styles["small"],
    )
    return KeepTogether([row, Spacer(1, 2 * mm), note])


def _brand_appendix_header(title: str, styles: dict) -> Table:
    t = Table([[Paragraph(_esc(title), styles["card_title"])]], colWidths=[_CONTENT_W])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), HexColor("#1b4332")),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return t


def _status_chip(text: str, *, bg: str, styles: dict) -> Table:
    cell = Paragraph(_esc(text), styles["chip"])
    t = Table([[cell]], colWidths=[40 * mm])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), HexColor(bg)),
                ("BOX", (0, 0), (-1, -1), 0.3, HexColor("#90a4ae")),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    return t


def render_season_growth_pdf(
    *,
    facts: dict[str, Any],
    ai: dict[str, Any] | None,
    chart_path: Path | str | None = None,
    chart_paths: dict[str, Path | str] | None = None,
    materials_meta: list[dict[str, Any]] | None,
    out_path: Path | str,
) -> Path:
    """Render exactly 10 product pages (Round-2 layout)."""
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
    s2_appendix = filter_s2_appendix_rows(list(facts.get("s2_appendix") or []))
    s1_appendix = filter_s1_appendix_rows(list(facts.get("s1_appendix") or []))
    confidence = facts.get("confidence") or {}
    status_cards = list(facts.get("status_cards") or [])
    evidence_cards = list(facts.get("evidence_cards") or [])
    yoy = facts.get("yoy") or {}
    program_core = facts.get("program_core_conclusion")
    program_conclusions_list = list(facts.get("program_conclusions") or [])
    disclaimer = facts.get("disclaimer") or FOOTER_DISCLAIMER
    ai = ai or {}
    charts = _resolve_chart_paths(chart_paths, chart_path)
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    spatial = facts.get("spatial") or {}

    doc = SimpleDocTemplate(
        str(out),
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=12 * mm,
        bottomMargin=14 * mm,
        title="生育期长势分析报告",
    )
    story: list[Any] = []

    # ── P1 地块当前状态总览 ──
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph("地块当前状态总览", styles["cover_title"]))
    story.append(
        Paragraph("遥感长势 · 水分 · 洪涝 · 成熟监测（程序事实 + AI 谨慎解读）", styles["cover_subtitle"])
    )
    story.append(Spacer(1, 2 * mm))
    field_name = field.get("field_name") or "地块"
    season_label = window.get("label") or ""
    window_s = f"{window.get('start_date') or '—'} ~ {window.get('end_date') or '—'}"
    if season_label:
        window_s += f"（{season_label}）"
    meta_line = (
        f"{field_name} · 编号 {field.get('land_id') or '—'} · "
        f"{_fmt_area(field.get('area_ha'))} · {_crops_label(window, field)} · "
        f"{window_s} · 报告 {now}"
    )
    story.append(Paragraph(_esc(meta_line), styles["small_c"]))
    story.append(Spacer(1, 3 * mm))
    story.append(_dual_visual_row(facts, styles, charts))
    story.append(Spacer(1, 3 * mm))
    story.append(_status_cards_table(status_cards))
    story.append(Spacer(1, 2 * mm))
    story.append(
        Paragraph(
            "置信度由程序规则给出（高/中/低），不是 AI 百分比。"
            f" 官方可用景 {scenes.get('s2_official_count', '—')} / 总 {scenes.get('s2_count', '—')}；"
            f"S1 {scenes.get('s1_count', '—')} 景。",
            styles["small"],
        )
    )
    story.append(PageBreak())

    # ── P2 本季综合研判 ──
    story.append(Paragraph("本季综合研判", styles["h1"]))
    core = ai.get("core_conclusion") or program_core or "（程序事实已生成）"
    story.append(_highlight_box("核心判断", str(core), styles))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph("关键证据", styles["h2"]))
    if evidence_cards:
        story.append(_evidence_cards_table(evidence_cards))
    else:
        story.append(Paragraph("（无程序证据卡）", styles["small"]))
    if charts.get("drought_grades"):
        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph("干旱等级分布（程序）", styles["h2"]))
        _chart_block(
            story,
            path=charts.get("drought_grades"),
            width_mm=120,
            height_mm=58,
            caption="图 干旱等级景数（重度/中度/轻度/正常，仅官方可用景）",
            missing="",
            styles=styles,
        )
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph("判断可信度", styles["h2"]))
    conf_items = list(confidence.get("items") or [])
    if not conf_items:
        for key in ("growth", "drought", "flood", "harvest"):
            block = confidence.get(key)
            if isinstance(block, dict):
                conf_items.append(block)
    if conf_items:
        story.append(_confidence_bars(conf_items, styles))
    else:
        story.append(Paragraph("（可信度块未生成）", styles["small"]))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph("智能研判", styles["h2"]))
    synthesis = ai.get("synthesis") or ai.get("interpretation") or ""
    if not synthesis:
        synthesis = (
            program_core
            or "大模型未启用。以下仅列程序事实，不作天气/播种/产量推断。"
        )
    story.append(_highlight_box("智能研判", str(synthesis), styles))
    story.append(PageBreak())

    # ── P3 长势与水分变化 ──
    story.append(Paragraph("长势与水分变化", styles["h1"]))
    _chart_block(
        story,
        path=charts.get("ndvi_ndmi"),
        width_mm=170,
        height_mm=72,
        caption="图1 NDVI（粗实线）/ NDMI（细虚线）：不可靠点浅空心不入趋势；底部色条为干旱事件。",
        missing="（NDVI/NDMI 曲线未生成）",
        styles=styles,
    )
    _chart_block(
        story,
        path=charts.get("s1_vv"),
        width_mm=170,
        height_mm=52,
        caption="图2 Sentinel-1 VV：阈值 -17.0 / -15.0 dB；橙色为关注。监测期未形成明确地表积水型洪涝信号。",
        missing="（S1 VV 曲线未生成）",
        styles=styles,
    )
    story.append(Paragraph("按月解读（至多两行）", styles["h2"]))
    month_notes = list(ai.get("monthly_notes") or ai.get("timeline_bullets") or [])
    for i, row in enumerate(timeline[:4]):
        label = row.get("period_label") or row.get("month") or f"{i+1}月"
        note = ""
        if i < len(month_notes):
            note = str(month_notes[i])
        else:
            note = (
                f"{label}：{row.get('s2_growth') or '—'}；"
                f"水分 {row.get('moisture') or '—'}；{row.get('s1_flood') or '—'}"
            )
        # max ~2 lines
        if len(note) > 90:
            note = note[:89] + "…"
        story.append(Paragraph(f"• {_esc(note)}", styles["bullet"]))
    if int(flood.get("flood_scene_count") or 0) <= 0:
        story.append(
            Paragraph(
                "监测期未形成明确地表积水型洪涝信号（程序）。",
                styles["small"],
            )
        )
    story.append(PageBreak())

    # ── P4 生育期关键时间线 ──
    story.append(Paragraph("生育期关键时间线", styles["h1"]))
    story.append(
        Paragraph(
            "按月横向故事线。作物阶段为日历估计，不是实测播种。已移除窗口覆盖表与全部干旱日一览。",
            styles["small"],
        )
    )
    story.append(Spacer(1, 2 * mm))
    # horizontal month story as 4 cards
    month_cards = []
    for row in timeline[:4]:
        body = [
            str(row.get("crop_stage_estimate") or "—"),
            str(row.get("s2_growth") or "—"),
            f"水分 {row.get('moisture') or '—'}",
            str(row.get("s1_flood") or "—"),
        ]
        month_cards.append(
            _panel_card(
                str(row.get("period_label") or row.get("month") or "月"),
                body,
                header_bg="#ffffff",
                body_bg="#ffffff",
                border="#1b4332",
                width=(_CONTENT_W - 6 * mm) / 4,
                styles=styles,
                title_color="#1b4332",
            )
        )
    while len(month_cards) < 4:
        month_cards.append("")
    story.append(
        Table([month_cards], colWidths=[(_CONTENT_W - 6 * mm) / 4] * 4)
    )
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph("年度峰值对比（仅峰值日期）", styles["h2"]))
    if charts.get("yoy_peak"):
        _chart_block(
            story,
            path=charts.get("yoy_peak"),
            width_mm=120,
            height_mm=58,
            caption="图 上年/本年 NDVI 峰值对比（标注峰值日期；提前/推后≠物候整体提前）",
            missing="",
            styles=styles,
        )
        shift = (yoy.get("peak_date_shift") or {}).get("label") or "峰值日期对比不可用"
        story.append(
            Paragraph(
                f"程序计算：{_esc(shift)}。峰值日期提前/推后 ≠ 物候进程提前相应天数，"
                "不得据此推断播种或积温。",
                styles["small"],
            )
        )
    else:
        story.append(_yoy_visual(yoy, styles))
    if charts.get("monthly_ndvi"):
        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph("月均 NDVI", styles["h2"]))
        _chart_block(
            story,
            path=charts.get("monthly_ndvi"),
            width_mm=130,
            height_mm=55,
            caption="图 窗口内月均 NDVI（官方/可用景）",
            missing="",
            styles=styles,
        )
    story.append(Spacer(1, 3 * mm))
    # Prefer charts over dense tables; keep a compact key-date strip only if no monthly chart
    key_dates = program_key_dates(ndvi=ndvi, drought=drought, harvest=harvest)
    if key_dates and not charts.get("monthly_ndvi"):
        story.append(Paragraph("本季关键日期（程序）", styles["h2"]))
        kd_rows = [["日期", "事件", "详情"]]
        for kd in key_dates[:6]:
            kd_rows.append(
                [
                    str(kd.get("date") or "—"),
                    str(kd.get("label") or kd.get("event") or "—"),
                    str(kd.get("detail") or "—"),
                ]
            )
        story.append(_table(kd_rows, col_widths=[32 * mm, 40 * mm, 106 * mm]))
    elif key_dates:
        story.append(Paragraph("本季关键日期（程序）", styles["h2"]))
        chips = "　".join(
            f"{kd.get('date') or '—'} {kd.get('label') or ''}"
            for kd in key_dates[:5]
        )
        story.append(Paragraph(_esc(chips), styles["small"]))
    story.append(PageBreak())

    # ── P5 空间长势与异常区域 ──
    story.append(Paragraph("空间长势与异常区域", styles["h1"]))
    has_pixels = bool(spatial.get("has_pixel_stats"))
    grade_shares = spatial.get("grade_shares") or {}
    if has_pixels and grade_shares.get("n"):
        pct = grade_shares.get("pct") or {}
        story.append(
            Paragraph(
                f"程序像元分级（n={grade_shares.get('n')}，"
                f"{spatial.get('pixel_date') or '—'}）："
                f"较好 {pct.get('较好', 0)}% / 正常 {pct.get('正常', 0)}% / "
                f"偏弱 {pct.get('偏弱', 0)}%。"
                f"{grade_shares.get('rule_zh') or ''}",
                styles["body"],
            )
        )
    else:
        story.append(
            Paragraph(
                spatial.get("note")
                or "当前版本暂未生成地块内部空间分级统计",
                styles["body"],
            )
        )
    story.append(Spacer(1, 2 * mm))
    story.append(
        Paragraph(
            "当前暂无可靠的空间异常聚集结论（不编造东南象限异常）。",
            styles["small"],
        )
    )
    story.append(Spacer(1, 3 * mm))

    # Dual maps: RGB + NDVI spatial
    rgb_path = (
        spatial.get("rgb_local_path")
        or spatial.get("latest_rgb_path")
        or charts.get("latest_rgb")
    )
    ndvi_path = (
        spatial.get("ndvi_local_path")
        or spatial.get("ndvi_map_path")
        or charts.get("ndvi_spatial")
    )
    peak_path = spatial.get("peak_rgb_path") or charts.get("peak_rgb")
    rgb_img = _try_image(rgb_path, max_w=_CONTENT_W / 2 - 4 * mm, max_h=70 * mm)
    ndvi_img = _try_image(ndvi_path, max_w=_CONTENT_W / 2 - 4 * mm, max_h=70 * mm)
    if rgb_img is None and peak_path:
        rgb_img = _try_image(peak_path, max_w=_CONTENT_W / 2 - 4 * mm, max_h=70 * mm)
    left = rgb_img or _placeholder_panel(
        "真彩下载失败或暂无 rgb_url", styles, height_mm=70
    )
    right = ndvi_img or _placeholder_panel(
        "NDVI 空间图暂不可用（像元不足或渲染失败）", styles, height_mm=70
    )
    rgb_date = spatial.get("latest_rgb_date") or spatial.get("peak_rgb_date") or "—"
    ndvi_date = spatial.get("pixel_date") or rgb_date
    maps = Table(
        [
            [left, right],
            [
                Paragraph(f"真彩预览（{rgb_date}）", styles["caption"]),
                Paragraph(f"NDVI 空间分布（{ndvi_date}）", styles["caption"]),
            ],
        ],
        colWidths=[_CONTENT_W / 2, _CONTENT_W / 2],
    )
    maps.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ]
        )
    )
    story.append(maps)
    story.append(Spacer(1, 3 * mm))
    if charts.get("growth_grades"):
        _chart_block(
            story,
            path=charts.get("growth_grades"),
            width_mm=110,
            height_mm=58,
            caption="图 像元长势等级占比（较好 / 正常 / 偏弱，仅程序像元）",
            missing="",
            styles=styles,
        )
    elif spatial.get("rgb_url") and rgb_img is None:
        story.append(
            Paragraph(
                "已登记 rgb_url，但本版 PDF 下载栅格预览失败。",
                styles["small"],
            )
        )
    story.append(Spacer(1, 2 * mm))
    story.append(
        Paragraph(
            "说明：像元为 lonlat_v1 稀疏点而非规则栅格；等级占比仅由程序像元计算，"
            "不编造分区百分比。",
            styles["small"],
        )
    )
    story.append(PageBreak())

    # ── P6 风险成因 ──
    story.append(Paragraph("风险成因", styles["h1"]))
    strong = _items_from_value(ai.get("factors_strong"))
    mid = _items_from_value(ai.get("factors_mid"))
    weak = _items_from_value(ai.get("factors_weak"))
    # Program facts only in 已观察到
    observed = []
    if drought.get("drought_scene_count"):
        observed.append(
            f"程序干旱景 {drought.get('drought_scene_count')}："
            f"{format_drought_counts(drought.get('counts'))}"
        )
    if int(flood.get("flood_scene_count") or 0) == 0 and scenes.get("s1_count"):
        observed.append(
            f"S1 {scenes.get('s1_count')} 景未检出洪涝："
            f"{format_flood_counts(flood.get('counts'))}"
        )
    peak = ndvi.get("peak") or {}
    latest = ndvi.get("latest") or {}
    if peak.get("date") and latest.get("date"):
        observed.append(
            f"NDVI 峰值 {peak.get('date')}（{_fmt(peak.get('value'), 3)}）→ "
            f"最新 {latest.get('date')}（{_fmt(latest.get('value'), 3)}）"
        )
    if not observed:
        observed = strong[:2] or ["程序可核验事实不足，仅保留谨慎提示。"]
    # Filter banned phrases from AI mid/weak
    banned = ("温光", "降水偏少", "排水良好", "排水条件良好", "无渍涝隐患", "生物量达标")
    mid = [x for x in mid if not any(b in x for b in banned)] or [
        "九月绿度回落与干旱等级共现时，成熟脱水与天气偏干可能同时存在（不能定量）。"
    ]
    weak = [x for x in weak if not any(b in x for b in banned)] or [
        "天气、播种、品种、土壤、产量、墒情均未由本系统观测，不能认定。"
    ]
    story.append(
        _factor_cards_row(observed[:4], mid[:4], weak[:4], styles)
    )
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph("程序综合结论（非 AI 数字）", styles["h2"]))
    conclusions = _items_from_value(ai.get("conclusions")) or program_conclusions_list
    # strip banned
    conclusions = [c for c in conclusions if not any(b in str(c) for b in banned)]
    if conclusions:
        story.append(_conclusion_pills([str(c) for c in conclusions[:4]], styles))
    else:
        story.append(Paragraph("（结论待生成）", styles["small"]))
    story.append(PageBreak())

    # ── P7 农事建议 ──
    story.append(Paragraph("农事建议", styles["h1"]))
    now_items = _items_from_value(ai.get("actions_now"))
    week_items = _items_from_value(ai.get("actions_week"))
    next_items = _items_from_value(ai.get("actions_next_season"))
    # End-of-season records card
    records = [
        "记录实测播种日期、品种与产量，便于校准物候估计。",
        "保留关键干旱/关注日期与田间照片，供下季对照。",
    ]
    # Rebuild 2x2 suggestion cards (unified white + brand)
    gap = 3 * mm
    col_w = (_CONTENT_W - gap) / 2
    c1 = _panel_card(
        "田间核查清单",
        now_items or ["田间确认成熟度、倒伏与墒情；不宜仅凭遥感安排作业。"],
        header_bg="#ffffff",
        body_bg="#ffffff",
        border="#1b4332",
        width=col_w,
        styles=styles,
        title_color="#1b4332",
    )
    c2 = _panel_card(
        "7日监测",
        week_items or ["关注墒情与植株脱水，结合气象安排农事。"],
        header_bg="#ffffff",
        body_bg="#ffffff",
        border="#1b4332",
        width=col_w,
        styles=styles,
        title_color="#1b4332",
    )
    c3 = _panel_card(
        "季末记录",
        records,
        header_bg="#ffffff",
        body_bg="#ffffff",
        border="#1b4332",
        width=col_w,
        styles=styles,
        title_color="#1b4332",
    )
    c4 = _panel_card(
        "下一季农艺",
        next_items
        or _items_from_value(
            program_next_season_actions(drought=drought, flood=flood, harvest=harvest)
        )
        or ["下一季在拔节–抽雄、灌浆阶段安排墒情检查与灌溉准备；雨季维护排水。"],
        header_bg="#ffffff",
        body_bg="#ffffff",
        border="#1b4332",
        width=col_w,
        styles=styles,
        title_color="#1b4332",
    )
    story.append(
        Table([[c1, c2], [c3, c4]], colWidths=[col_w, col_w])
    )
    story.append(Spacer(1, 4 * mm))
    if harvest.get("status") == "detected":
        harvest_hint = (
            "收获注意：疑似进入成熟后期或收获准备阶段，需田间确认，不得作为立即收割依据。"
        )
    else:
        harvest_hint = "收获注意：收获安排须田间确认，不得作为立即收割依据。"
    story.append(_caution_banner(harvest_hint, styles))
    story.append(Spacer(1, 3 * mm))
    gaps = _items_from_value(ai.get("evidence_gaps"))
    if gaps:
        story.append(Paragraph("仍需补充的证据", styles["h2"]))
        _bullets(story, gaps[:5], styles)
    story.append(PageBreak())

    # ── P8 技术附录 A S2 ──
    story.append(_brand_appendix_header("技术附录 A · Sentinel-2 逐景表", styles))
    story.append(Spacer(1, 2 * mm))
    chip_row = Table(
        [[
            _status_chip(f"S2总景 {scenes.get('s2_count', '—')}", bg="#e8f5e9", styles=styles),
            _status_chip(
                f"官方/可用 {scenes.get('s2_official_count', '—')}",
                bg="#e3f2fd",
                styles=styles,
            ),
            _status_chip(
                f"干旱景 {drought.get('drought_scene_count', '—')}",
                bg="#fff3e0",
                styles=styles,
            ),
            _status_chip(
                format_drought_counts(drought.get("counts"))[:18],
                bg="#fce4ec",
                styles=styles,
            ),
        ]],
        colWidths=[44.5 * mm] * 4,
    )
    story.append(chip_row)
    story.append(Spacer(1, 2 * mm))
    if not s2_appendix:
        story.append(Paragraph("无 S2 逐景记录。", styles["body"]))
    else:
        # Keep appendix A on a single page: prefer drought days then chronological.
        s2_max = 26
        drought_dates = {
            str(d.get("date"))[:10]
            for d in (drought.get("days") or [])
            if d.get("date")
        }
        preferred = [r for r in s2_appendix if str(r.get("date") or "")[:10] in drought_dates]
        rest = [r for r in s2_appendix if str(r.get("date") or "")[:10] not in drought_dates]
        ordered = preferred + rest
        truncated = len(ordered) > s2_max
        rows_a = ordered[:s2_max]
        s2_rows = [["日期", "云量%", "质量", "干旱等级", "NDVI", "NDMI", "EVI", "MNDWI"]]
        for r in rows_a:
            cls = r.get("drought_class_cn") or drought_class_cn(r.get("drought_class"))
            q_label = r.get("quality_cn") or quality_cn(r.get("quality"))
            s2_rows.append(
                [
                    str(r.get("date") or "—"),
                    _fmt(r.get("cloud_pct"), 1),
                    str(q_label),
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
                    18 * mm,
                    18 * mm,
                    22 * mm,
                    20 * mm,
                    20 * mm,
                    20 * mm,
                    22 * mm,
                ],
                numeric_cols={1, 4, 5, 6, 7},
                header_bg="#1b4332",
            )
        )
        # fix header text color for dark brand — recolor via note
        if truncated:
            story.append(
                Paragraph(
                    f"注：筛选后共 {len(s2_appendix)} 行，附录仅展示前 {len(rows_a)} 行（优先干旱日）。数值右对齐。",
                    styles["small"],
                )
            )
        else:
            story.append(
                Paragraph(
                    "注：同日多景已按质量优选；数值列右对齐。",
                    styles["small"],
                )
            )
    story.append(PageBreak())

    # ── P9 技术附录 B S1 + overview cards ──
    story.append(_brand_appendix_header("技术附录 B · Sentinel-1 逐景表", styles))
    story.append(Spacer(1, 2 * mm))
    s1_overview = Table(
        [[
            _panel_card(
                "S1 景数",
                [str(scenes.get("s1_count") or flood.get("scene_count") or 0)],
                header_bg="#ffffff",
                body_bg="#ffffff",
                border="#1565c0",
                width=(_CONTENT_W - 6 * mm) / 3,
                styles=styles,
                title_color="#1565c0",
            ),
            _panel_card(
                "VV 中位数",
                [str(flood.get("vv_median") if flood.get("vv_median") is not None else "—")],
                header_bg="#ffffff",
                body_bg="#ffffff",
                border="#1565c0",
                width=(_CONTENT_W - 6 * mm) / 3,
                styles=styles,
                title_color="#1565c0",
            ),
            _panel_card(
                "洪涝状态",
                [
                    format_flood_status(flood.get("status")),
                    format_flood_counts(flood.get("counts")),
                ],
                header_bg="#ffffff",
                body_bg="#ffffff",
                border="#1565c0",
                width=(_CONTENT_W - 6 * mm) / 3,
                styles=styles,
                title_color="#1565c0",
            ),
        ]],
        colWidths=[(_CONTENT_W - 6 * mm) / 3] * 3,
    )
    story.append(s1_overview)
    story.append(Spacer(1, 2 * mm))
    if charts.get("s1_status"):
        _chart_block(
            story,
            path=charts.get("s1_status"),
            width_mm=100,
            height_mm=52,
            caption="图 S1 状态分布（正常 / 关注 / 洪涝）",
            missing="",
            styles=styles,
        )
    story.append(Spacer(1, 2 * mm))
    if int(flood.get("flood_scene_count") or 0) <= 0:
        story.append(
            Paragraph(
                "监测期未形成明确地表积水型洪涝信号。",
                styles["body"],
            )
        )
    story.append(Spacer(1, 2 * mm))
    if not s1_appendix:
        story.append(Paragraph("无 S1 逐景记录。", styles["body"]))
    else:
        s1_max = 18
        truncated_b = len(s1_appendix) > s1_max
        rows_b = s1_appendix[:s1_max]
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
                col_widths=[32 * mm, 26 * mm, 32 * mm, 32 * mm, 40 * mm],
                numeric_cols={1, 2, 3},
                header_bg="#1565c0",
            )
        )
        if truncated_b:
            story.append(
                Paragraph(
                    f"注：去重后共 {len(s1_appendix)} 行，附录仅展示前 {len(rows_b)} 行。",
                    styles["small"],
                )
            )
        else:
            story.append(Paragraph("注：同日多景已按较低 VV 保留一条；数值右对齐。", styles["small"]))
    story.append(PageBreak())

    # ── P10 方法 + 质量 + 声明 ──
    story.append(Paragraph("方法与质量说明", styles["h1"]))
    method_rows = [
        ["类别", "说明"],
        ["光学干旱（S2）", methodology.get("drought") or "基于 agri_classify 干旱分类器。"],
        ["SAR 洪涝（S1）", methodology.get("flood") or "基于 agri_classify 洪涝分类器。"],
        ["传感器", methodology.get("sensors") or "Sentinel-2 / Sentinel-1"],
        [
            "曲线规则",
            "NDVI 粗实线、NDMI 细虚线；不可靠点浅空心不连线；干旱为底部事件条。",
        ],
        [
            "水分月统计",
            "按日去重后的官方/可用景分类汇总；与 drought.days 不一致时以场景重聚合为准。",
        ],
        [
            "物候",
            "作物阶段为日历典型估计，标注「估计」，不代表实测播种日期。",
        ],
        [
            "收获",
            "观察型检测；低置信度仅提示疑似成熟后期或收获准备，需田间确认。",
        ],
    ]
    story.append(_table(method_rows, col_widths=[32 * mm, 146 * mm], zebra=True))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph("质量卡片", styles["h2"]))
    q_cards = Table(
        [[
            _panel_card(
                "光学覆盖",
                [
                    f"S2 总景 {scenes.get('s2_count', '—')}",
                    f"官方/可用 {scenes.get('s2_official_count', '—')}",
                    f"晴空 {scenes.get('s2_clear_count', '—')}",
                ],
                header_bg="#ffffff",
                body_bg="#ffffff",
                border="#1b4332",
                width=(_CONTENT_W - 6 * mm) / 3,
                styles=styles,
                title_color="#1b4332",
            ),
            _panel_card(
                "干旱分级",
                [format_drought_counts(drought.get("counts"))],
                header_bg="#ffffff",
                body_bg="#ffffff",
                border="#c62828",
                width=(_CONTENT_W - 6 * mm) / 3,
                styles=styles,
                title_color="#c62828",
            ),
            _panel_card(
                "洪涝 / 收获",
                [
                    f"{format_flood_status(flood.get('status'))}；{format_flood_counts(flood.get('counts'))}",
                    format_harvest_line(harvest),
                ],
                header_bg="#ffffff",
                body_bg="#ffffff",
                border="#1565c0",
                width=(_CONTENT_W - 6 * mm) / 3,
                styles=styles,
                title_color="#1565c0",
            ),
        ]],
        colWidths=[(_CONTENT_W - 6 * mm) / 3] * 3,
    )
    story.append(q_cards)
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(_esc(disclaimer), styles["footer"]))
    mats = materials_meta or []
    if mats:
        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph("附加材料", styles["h2"]))
        for m in mats[:6]:
            line = f"• {_esc(m.get('filename'))}"
            if m.get("note"):
                line += f"（{_esc(m.get('note'))}）"
            story.append(Paragraph(line, styles["bullet"]))

    doc.build(story, onFirstPage=_page_footer, onLaterPages=_page_footer)
    return out
